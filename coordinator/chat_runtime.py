"""Chat room turn control and the per-participant provider call (GH #149).

Turn exclusivity is the ``VoiceConversation.busy_until`` lease, re-used verbatim:
the lease identity is the exact ``turn_busy_until`` value, so a turn proves
ownership by matching its own deadline, an expired lease is never resurrected,
and every persisting write is lease-guarded instead of a blind save.

Unlike the voice conversation, a room runs exactly one speaker per request. Six
participants at the 45 s provider timeout would be a 270 s HTTP request, so the
round-robin order is persisted in ``ChatRoom.pending_speakers`` and the client
calls ``advance`` once per speaker. A speaker is popped only when its message
commits, so a crash mid-turn retries that speaker instead of skipping it.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import exceptions

from . import chat_policy, services
from .conversation_limits import json_bytes
from .dbretry import retry_sqlite
from .models import ChatMessage, ChatParticipant, ChatRoom
from .voice_runtime import provider_request, response_json


def limits():
    return {
        "max_participants": settings.CHAT_ROOM_MAX_PARTICIPANTS,
        "max_rounds": settings.CHAT_ROOM_MAX_ROUNDS,
        "max_tool_rounds": settings.CHAT_ROOM_MAX_TOOL_ROUNDS,
        "context_messages": settings.CHAT_ROOM_CONTEXT_MESSAGES,
        "context_bytes": settings.CHAT_ROOM_CONTEXT_BYTES,
        "max_output_tokens": settings.CHAT_ROOM_MAX_OUTPUT_TOKENS,
        "max_messages": settings.CHAT_ROOM_MAX_MESSAGES,
        "max_history_bytes": settings.CHAT_ROOM_MAX_HISTORY_BYTES,
        "page_limit": settings.CHAT_ROOM_PAGE_LIMIT,
        "context_accounting": "conservative_utf8_bytes",
    }


def full(name, maximum):
    raise services.TaskError(
        f"Chat room {name} limit ({maximum}) reached; saved history and completed "
        "tool actions are preserved. Close this room and start another; "
        "check task state before repeating any action.",
        409,
    )


def check_room(room, *, reserve=1):
    """Refuse a turn at a hard cap. Nothing is ever pruned to make space."""
    if room.message_count + reserve > settings.CHAT_ROOM_MAX_MESSAGES:
        full("message count", settings.CHAT_ROOM_MAX_MESSAGES)
    if room.history_bytes > settings.CHAT_ROOM_MAX_HISTORY_BYTES:
        full("history bytes", settings.CHAT_ROOM_MAX_HISTORY_BYTES)


def check_context(payload):
    # One token per UTF-8 byte is a deliberately conservative upper bound, as in
    # conversation_limits.check_context; the room keeps its own output reserve.
    bound = json_bytes(payload) + 256 * (len(payload["messages"]) + len(payload["tools"]))
    if bound + settings.CHAT_ROOM_MAX_OUTPUT_TOKENS > settings.PM_CONTEXT_WINDOW_TOKENS:
        full("context token budget (conservative estimate)", settings.PM_CONTEXT_WINDOW_TOKENS)


def speakers_for(room, rounds):
    """Round-robin expansion: unmuted participants in position order, per round."""
    roster = list(room.participants.filter(is_muted=False).order_by("position", "pk"))
    if not roster:
        raise services.TaskError("Add an unmuted participant before starting a turn", 409)
    return [participant.pk for _ in range(rounds) for participant in roster]


def history_window(room):
    """Newest messages that fit the prompt window, returned oldest first.

    The window bounds the prompt only. Every message stays readable through the
    history endpoint; dropping the oldest here never deletes anything.
    """
    rows = list(room.messages.order_by("-seq")[: settings.CHAT_ROOM_CONTEXT_MESSAGES])
    budget = settings.CHAT_ROOM_CONTEXT_BYTES
    kept = []
    for message in rows:
        cost = len(message.text.encode("utf-8")) + len(message.role_label.encode("utf-8")) + 16
        if kept and budget - cost < 0:
            break
        budget -= cost
        kept.append(message)
    return list(reversed(kept))


def speaker_label(message):
    if message.author_kind == ChatMessage.Author.PARTICIPANT:
        return message.role_label or "participant"
    if message.author_kind == ChatMessage.Author.SYSTEM:
        return "system"
    return message.role_label or "human"


def render_history(messages, participant):
    """Render the shared transcript from one participant's point of view.

    The speaker's own lines are its own assistant turns; everybody else's are
    labelled user lines, with consecutive ones merged so the provider sees an
    alternating conversation rather than a run of user messages.
    """
    rendered = []
    for message in messages:
        text = message.text.strip()
        if not text:
            continue
        own = (
            message.author_kind == ChatMessage.Author.PARTICIPANT
            and message.participant_id == participant.pk
        )
        if own:
            rendered.append({"role": "assistant", "content": text})
            continue
        line = f"{speaker_label(message)}: {text}"
        if rendered and rendered[-1]["role"] == "user":
            rendered[-1]["content"] = f"{rendered[-1]['content']}\n{line}"
        else:
            rendered.append({"role": "user", "content": line})
    return rendered


@retry_sqlite
@transaction.atomic
def post_message(room, user, text, rounds=None):
    """Persist a human message and plan the turn. No provider call happens here."""
    current = services.locked(ChatRoom.objects.filter(pk=room.pk)).first()
    if current is None:
        raise Http404
    if current.status != ChatRoom.Status.OPEN:
        raise services.TaskError("This room is closed", 409)
    if current.turn_busy_until and current.turn_busy_until > timezone.now():
        raise services.TaskError("A room turn is already in progress", 409)
    if current.pending_speakers:
        raise services.TaskError("Finish or reset the current turn first", 409)
    rounds = min(max(int(rounds or current.rounds_per_turn), 1), settings.CHAT_ROOM_MAX_ROUNDS)
    pending = speakers_for(current, rounds)
    check_room(current, reserve=1 + len(pending))
    current.turn_seq += 1
    message = append_message(
        current,
        author_kind=ChatMessage.Author.HUMAN,
        text=text,
        author_user=user,
        role_label=user.get_username(),
    )
    current.pending_speakers = pending
    current.save(update_fields=["turn_seq", "pending_speakers", "updated_at"])
    return current, message


def append_message(
    room,
    *,
    author_kind,
    text,
    participant=None,
    author_user=None,
    role_label="",
    tool_events=None,
    model=None,
):
    """Create the next message in sequence and move the room's counters.

    The caller must already hold the room row lock; ``seq`` and the counters are
    only ever derived under it, so they cannot drift from the rows.
    """
    message = ChatMessage.objects.create(
        room=room,
        seq=room.message_count + 1,
        turn=room.turn_seq,
        author_kind=author_kind,
        participant=participant,
        author_user=author_user,
        role_label=role_label[:60],
        text=text,
        tool_events=tool_events or [],
        model=model,
    )
    room.message_count = message.seq
    room.history_bytes += len(text.encode("utf-8"))
    room.save(update_fields=["message_count", "history_bytes", "updated_at"])
    return message


@retry_sqlite
@transaction.atomic
def begin_turn(room, rounds=None, reset=False):
    """Acquire the room turn lease for the next pending speaker.

    Returns ``(room, participant)``, or ``(room, None)`` when nothing is pending.
    The speaker is not popped here: it is popped when its reply commits.
    """
    current = services.locked(ChatRoom.objects.filter(pk=room.pk)).first()
    if current is None:
        raise Http404
    if current.status != ChatRoom.Status.OPEN:
        raise services.TaskError("This room is closed", 409)
    now = timezone.now()
    if reset:
        current.pending_speakers = []
        current.turn_busy_until = None
        current.save(update_fields=["pending_speakers", "turn_busy_until", "updated_at"])
        return current, None
    if current.turn_busy_until and current.turn_busy_until > now:
        raise services.TaskError("A room turn is already in progress", 409)
    if not current.pending_speakers and rounds:
        current.turn_seq += 1
        current.pending_speakers = speakers_for(
            current, min(max(int(rounds), 1), settings.CHAT_ROOM_MAX_ROUNDS)
        )
        current.save(update_fields=["turn_seq", "pending_speakers", "updated_at"])
    if not current.pending_speakers:
        return current, None
    check_room(current)
    participant = current.participants.filter(pk=current.pending_speakers[0]).first()
    if participant is None or participant.is_muted:
        # A participant removed or muted mid-turn is dropped, not retried forever.
        current.pending_speakers = current.pending_speakers[1:]
        current.save(update_fields=["pending_speakers", "updated_at"])
        return current, None
    current.turn_busy_until = now + timedelta(seconds=settings.CHAT_ROOM_TURN_LEASE_SECONDS)
    current.save(update_fields=["turn_busy_until", "updated_at"])
    return current, participant


@retry_sqlite
@transaction.atomic
def renew_turn(room):
    """Extend this turn's lease, refusing if it expired or was replaced."""
    current = services.locked(ChatRoom.objects.filter(pk=room.pk)).first()
    if current is None:
        raise exceptions.NotFound("This chat room no longer exists")
    now = timezone.now()
    if (
        current.turn_busy_until is None
        or current.turn_busy_until != room.turn_busy_until
        or current.turn_busy_until <= now
    ):
        raise services.TaskError("This room turn no longer owns the room", 409)
    if current.status != ChatRoom.Status.OPEN:
        raise services.TaskError("This room is closed", 409)
    current.turn_busy_until = now + timedelta(seconds=settings.CHAT_ROOM_TURN_LEASE_SECONDS)
    current.save(update_fields=["turn_busy_until", "updated_at"])
    room.turn_busy_until = current.turn_busy_until


def release_turn(room):
    """Clear the lease only if this turn still holds it."""
    ChatRoom.objects.filter(pk=room.pk, turn_busy_until=room.turn_busy_until).update(
        turn_busy_until=None
    )


@retry_sqlite
@transaction.atomic
def commit_reply(room, participant, text, tool_events, model):
    """Persist a participant's reply and pop it from the pending queue.

    Lease-guarded: a turn whose lease was stolen or expired writes nothing.
    """
    current = services.locked(ChatRoom.objects.filter(pk=room.pk)).first()
    if current is None:
        raise services.TaskError("This room turn no longer owns the room", 409)
    if (
        current.turn_busy_until is None
        or current.turn_busy_until != room.turn_busy_until
        or current.status != ChatRoom.Status.OPEN
    ):
        raise services.TaskError("This room turn no longer owns the room", 409)
    message = append_message(
        current,
        author_kind=ChatMessage.Author.PARTICIPANT,
        text=text,
        participant=participant,
        role_label=participant.role,
        tool_events=tool_events,
        model=model,
    )
    pending = list(current.pending_speakers)
    if pending and pending[0] == participant.pk:
        pending.pop(0)
        current.pending_speakers = pending
        current.save(update_fields=["pending_speakers", "updated_at"])
    return message


def build_payload(room, participant):
    roster = list(room.participants.order_by("position", "pk"))
    payload = {
        "model": (participant.model or {}).get("modelID"),
        "messages": [
            {"role": "system", "content": chat_policy.system_prompt(room, participant, roster)},
            *render_history(history_window(room), participant),
        ],
        "tools": chat_policy.tool_configs(participant),
        "tool_choice": "auto",
        "reasoning_effort": "none",
        "temperature": 0.6,
        "max_tokens": settings.CHAT_ROOM_MAX_OUTPUT_TOKENS,
    }
    check_context(payload)
    return payload


def parse_completion(data):
    """Validate a completion exactly as the voice runtime does."""
    try:
        choice = data["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        if not isinstance(content, str) or not isinstance(calls, list):
            raise ValueError
        truncated = choice.get("finish_reason") == "length"
        if not truncated and choice.get("finish_reason") not in {"stop", "tool_calls"}:
            raise ValueError
        parsed = []
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"])
            if (
                not isinstance(args, dict)
                or not isinstance(call["id"], str)
                or not isinstance(name, str)
            ):
                raise ValueError
            parsed.append((call["id"], name, args))
        if len({item[0] for item in parsed}) != len(parsed):
            raise ValueError
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        raise services.TaskError("Fireworks returned an invalid or incomplete reply", 502) from None
    return content, parsed, truncated


def speak(room, participant, execute):
    """Run one participant's turn against the provider and persist its reply.

    ``execute(participant, tool, args)`` performs a tool call; it is injected so
    the runtime never reaches into the tool authorization path directly.
    """
    messages = None
    events = []
    rounds = 0
    while True:
        renew_turn(room)
        payload = build_payload(room, participant)
        if messages is not None:
            payload["messages"] = messages
            check_context(payload)
        data = response_json(
            provider_request("Fireworks", "chat/completions", json=payload), "Fireworks"
        )
        renew_turn(room)
        content, calls, truncated = parse_completion(data)
        if truncated:
            # Never execute truncated tool arguments. Keep whatever was said.
            if content.strip():
                commit_reply(room, participant, content, events, participant.model)
            raise services.TaskError(
                "Fireworks reached the configured output token limit; the reply is "
                "incomplete. Received text and completed tool actions are preserved; "
                "check task state before continuing.",
                502,
            )
        messages = [*payload["messages"]]
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for call_id, name, args in calls
            ]
        messages.append(assistant)
        if not calls:
            if not content.strip():
                raise services.TaskError("Fireworks returned an empty reply", 502)
            return commit_reply(room, participant, content, events, participant.model)
        rounds += 1
        if rounds > settings.CHAT_ROOM_MAX_TOOL_ROUNDS:
            text = content.strip() or (
                f"[stopped after {settings.CHAT_ROOM_MAX_TOOL_ROUNDS} tool rounds]"
            )
            return commit_reply(room, participant, text, events, participant.model)
        for call_id, name, args in calls:
            renew_turn(room)
            try:
                result = execute(participant, name, args)
                failed = False
            except (
                services.TaskError,
                exceptions.APIException,
                Http404,
                ObjectDoesNotExist,
            ) as error:
                if isinstance(
                    error, (exceptions.AuthenticationFailed, exceptions.PermissionDenied)
                ):
                    raise
                # Errors are tool results, never fabricated successful mutations.
                result = {
                    "error": str(error)
                    if isinstance(error, services.TaskError)
                    else str(getattr(error, "detail", "Requested object not found"))
                }
                failed = True
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}
            )
            events.append({"tool": name, "is_error": failed})


def advance(room, execute, rounds=None, reset=False):
    """Run exactly one pending speaker. Returns the new message, or None."""
    room, participant = begin_turn(room, rounds=rounds, reset=reset)
    if participant is None:
        return room, None
    try:
        if participant.kind == ChatParticipant.Kind.WORKER:
            # Worker-backed participants are GH #150; refuse rather than pretend.
            raise services.TaskError(
                "Worker-backed participants are not implemented yet; mute this "
                "participant or give it a provider model",
                501,
            )
        return room, speak(room, participant, execute)
    finally:
        release_turn(room)
