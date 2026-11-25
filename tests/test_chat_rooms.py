"""Chat room data model, turn-taking, turn lease and history contract (GH #149)."""

import json
import threading

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import UserProfile
from coordinator import chat_runtime
from coordinator.models import Agent as AgentModel
from coordinator.models import ChatMessage, ChatParticipant, ChatRoom, Project, Worker

pytestmark = pytest.mark.django_db

MODEL = "accounts/fireworks/models/minimax-m3p1-flash"


@pytest.fixture
def owner(db):
    user = get_user_model().objects.create_user("room-owner", password="test-password")
    UserProfile.objects.update_or_create(user=user, defaults={"role": "owner"})
    return user


@pytest.fixture
def rooms(owner, project, settings):
    settings.FIREWORKS_API_KEY = "fireworks-server-secret"
    settings.CHAT_ROOM_MODELS = (MODEL, "accounts/fireworks/models/other-model")
    client = Client()
    client.force_login(owner)
    return client


def make_room(client, project, name="Design review", **extra):
    response = client.post(
        "/api/v1/chat/rooms",
        {"project": project.slug, "name": name, **extra},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return response.json()


def add_participant(client, room_id, role, position=None, can_act=False, **extra):
    payload = {
        "role": role,
        "persona": f"You are the {role}.",
        "model": {"providerID": "fireworks", "modelID": MODEL},
        "canAct": can_act,
        **extra,
    }
    if position is not None:
        payload["position"] = position
    response = client.post(
        f"/api/v1/chat/rooms/{room_id}/participants",
        payload,
        content_type="application/json",
    )
    return response


def completion(content=None, calls=None, finish_reason=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish_reason or ("tool_calls" if calls else "stop"),
                    "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                }
            ]
        },
    )


def tool_call(name, args, call_id="call_fixture"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def stub(monkeypatch, replies):
    """Record every outbound provider payload and serve canned completions."""
    requests = []
    stream = iter(replies)

    def provider(method, url, **kwargs):
        requests.append((url, kwargs))
        return next(stream)

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    return requests


def post(client, room_id, text, **extra):
    return client.post(
        f"/api/v1/chat/rooms/{room_id}/messages",
        {"text": text, **extra},
        content_type="application/json",
    )


def advance(client, room_id, **extra):
    return client.post(
        f"/api/v1/chat/rooms/{room_id}/advance", extra, content_type="application/json"
    )


def test_room_and_participants_crud_with_roster(rooms, project):
    room = make_room(rooms, project, topic="Ship the chat room")
    assert room["status"] == "open"
    assert room["participants"] == []
    assert add_participant(rooms, room["id"], "Architect", can_act=True).status_code == 201
    assert add_participant(rooms, room["id"], "Reviewer").status_code == 201
    detail = rooms.get(f"/api/v1/chat/rooms/{room['id']}").json()
    assert [item["role"] for item in detail["participants"]] == ["Architect", "Reviewer"]
    assert [item["position"] for item in detail["participants"]] == [1, 2]
    assert [item["canAct"] for item in detail["participants"]] == [True, False]
    renamed = rooms.patch(
        f"/api/v1/chat/rooms/{room['id']}",
        {"topic": "Updated topic", "roundsPerTurn": 2},
        content_type="application/json",
    )
    assert renamed.status_code == 200
    assert renamed.json()["topic"] == "Updated topic"
    assert renamed.json()["roundsPerTurn"] == 2
    closed = rooms.delete(f"/api/v1/chat/rooms/{room['id']}")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    # Closing retains history and the room row.
    assert ChatRoom.objects.get(pk=room["id"]).status == "closed"


def test_participant_validation_refuses_bad_bindings(rooms, project, settings):
    room = make_room(rooms, project)
    assert add_participant(rooms, room["id"], "Architect").status_code == 201
    # Duplicate role and duplicate position are both refused.
    assert add_participant(rooms, room["id"], "Architect").status_code == 400
    assert add_participant(rooms, room["id"], "Other", position=1).status_code == 400
    # A model outside the allowlist is refused.
    off_list = rooms.post(
        f"/api/v1/chat/rooms/{room['id']}/participants",
        {
            "role": "Stranger",
            "model": {"providerID": "fireworks", "modelID": "not-allowlisted"},
        },
        content_type="application/json",
    )
    assert off_list.status_code == 400
    assert "one of" in str(off_list.content)
    # A non-Fireworks provider is refused for a provider participant.
    other_provider = rooms.post(
        f"/api/v1/chat/rooms/{room['id']}/participants",
        {"role": "Stranger", "model": {"providerID": "anthropic", "modelID": MODEL}},
        content_type="application/json",
    )
    assert other_provider.status_code == 400
    # A missing model is refused.
    assert (
        rooms.post(
            f"/api/v1/chat/rooms/{room['id']}/participants",
            {"role": "Stranger"},
            content_type="application/json",
        ).status_code
        == 400
    )
    # A worker participant needs a worker.
    assert (
        rooms.post(
            f"/api/v1/chat/rooms/{room['id']}/participants",
            {"role": "Stranger", "kind": "worker", "model": {"providerID": "x", "modelID": "y"}},
            content_type="application/json",
        ).status_code
        == 400
    )
    settings.CHAT_ROOM_MAX_PARTICIPANTS = 2
    assert add_participant(rooms, room["id"], "Reviewer").status_code == 201
    capped = add_participant(rooms, room["id"], "Third")
    assert capped.status_code == 409
    assert "participants limit (2)" in capped.json()["detail"]


def test_database_refuses_a_second_acting_participant(rooms, project):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect", can_act=True)
    add_participant(rooms, room["id"], "Reviewer")
    reviewer = ChatParticipant.objects.get(role="Reviewer")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ChatParticipant.objects.filter(pk=reviewer.pk).update(can_act=True)


def test_setting_can_act_moves_the_single_actor(rooms, project):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect", can_act=True)
    add_participant(rooms, room["id"], "Reviewer")
    reviewer = ChatParticipant.objects.get(role="Reviewer")
    moved = rooms.patch(
        f"/api/v1/chat/rooms/{room['id']}/participants/{reviewer.pk}",
        {"canAct": True},
        content_type="application/json",
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["canAct"] is True
    assert ChatParticipant.objects.filter(can_act=True).count() == 1
    assert ChatParticipant.objects.get(can_act=True).role == "Reviewer"


def test_reordering_positions_swaps_without_violating_the_constraint(rooms, project):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    architect = ChatParticipant.objects.get(role="Architect")
    swapped = rooms.patch(
        f"/api/v1/chat/rooms/{room['id']}/participants/{architect.pk}",
        {"position": 2},
        content_type="application/json",
    )
    assert swapped.status_code == 200, swapped.content
    order = list(ChatParticipant.objects.order_by("position").values_list("role", flat=True))
    assert order == ["Reviewer", "Architect"]


def test_round_robin_runs_one_speaker_per_advance_in_position_order(rooms, project, monkeypatch):
    room = make_room(rooms, project, topic="Pick a database")
    for index, role in enumerate(["Architect", "Reviewer", "Tester"], start=1):
        assert add_participant(rooms, room["id"], role, position=index).status_code == 201
    requests = stub(
        monkeypatch,
        [
            completion("Postgres."),
            completion("Agreed, with caveats."),
            completion("I will test it."),
        ],
    )
    posted = post(rooms, room["id"], "Which database?")
    assert posted.status_code == 201, posted.content
    assert len(posted.json()["room"]["pendingSpeakers"]) == 3
    assert not requests, "posting a message must not call the provider"
    texts = []
    for _ in range(3):
        response = advance(rooms, room["id"])
        assert response.status_code == 200, response.content
        assert response.json()["advanced"] is True
        texts.append((response.json()["message"]["role"], response.json()["message"]["text"]))
    assert texts == [
        ("Architect", "Postgres."),
        ("Reviewer", "Agreed, with caveats."),
        ("Tester", "I will test it."),
    ]
    # A further advance is an idempotent no-op, not an error.
    spent = advance(rooms, room["id"])
    assert spent.status_code == 200
    assert spent.json()["advanced"] is False
    assert spent.json()["message"] is None
    assert len(requests) == 3
    seqs = list(ChatMessage.objects.order_by("seq").values_list("seq", flat=True))
    assert seqs == [1, 2, 3, 4], "seq must be gapless and start at the human message"
    assert ChatRoom.objects.get(pk=room["id"]).message_count == 4


def test_rounds_and_muting_shape_the_speaker_queue(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    muted = ChatParticipant.objects.get(role="Reviewer")
    rooms.patch(
        f"/api/v1/chat/rooms/{room['id']}/participants/{muted.pk}",
        {"isMuted": True},
        content_type="application/json",
    )
    stub(monkeypatch, [completion("One."), completion("Two.")])
    posted = post(rooms, room["id"], "Go", rounds=2)
    assert posted.status_code == 201
    # Two rounds over one unmuted participant, with the muted one skipped.
    assert len(posted.json()["room"]["pendingSpeakers"]) == 2
    for _ in range(2):
        assert advance(rooms, room["id"]).json()["advanced"] is True
    roles = list(
        ChatMessage.objects.filter(author_kind="participant").values_list("role_label", flat=True)
    )
    assert roles == ["Architect", "Architect"]
    assert "Reviewer" not in roles


def test_rounds_are_capped_by_the_setting(rooms, project, monkeypatch, settings):
    settings.CHAT_ROOM_MAX_ROUNDS = 2
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    stub(monkeypatch, [completion("Hi.")] * 5)
    posted = post(rooms, room["id"], "Go", rounds=9)
    assert len(posted.json()["room"]["pendingSpeakers"]) == 2


def test_a_second_concurrent_advance_is_refused_and_writes_nothing(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    stub(monkeypatch, [completion("Only one.")])
    post(rooms, room["id"], "Go")
    # Simulate a live lease held by another in-flight turn.
    ChatRoom.objects.filter(pk=room["id"]).update(
        turn_busy_until=timezone.now() + chat_runtime.timedelta(seconds=300)
    )
    blocked = advance(rooms, room["id"])
    assert blocked.status_code == 409
    assert "already in progress" in blocked.json()["detail"]
    assert not ChatMessage.objects.filter(author_kind="participant").exists()


def test_a_turn_whose_lease_was_stolen_aborts_without_writing(rooms, project, monkeypatch):
    room_data = make_room(rooms, project)
    add_participant(rooms, room_data["id"], "Architect")
    post(rooms, room_data["id"], "Go")

    def steal(method, url, **kwargs):
        # A successor overwrites the lease while this turn waits on the provider.
        ChatRoom.objects.filter(pk=room_data["id"]).update(
            turn_busy_until=timezone.now() + chat_runtime.timedelta(seconds=900)
        )
        return completion("I lost the turn.")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", steal)
    lost = advance(rooms, room_data["id"])
    assert lost.status_code == 409
    assert "no longer owns the room" in lost.json()["detail"]
    assert not ChatMessage.objects.filter(author_kind="participant").exists()
    # The loser must not clear the successor's lease.
    assert ChatRoom.objects.get(pk=room_data["id"]).turn_busy_until is not None


def test_an_expired_lease_is_not_resurrected(rooms, project, monkeypatch):
    room_data = make_room(rooms, project)
    add_participant(rooms, room_data["id"], "Architect")
    post(rooms, room_data["id"], "Go")

    def expire(method, url, **kwargs):
        ChatRoom.objects.filter(pk=room_data["id"]).update(
            turn_busy_until=timezone.now() - chat_runtime.timedelta(seconds=1)
        )
        return completion("Too late.")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", expire)
    stale = advance(rooms, room_data["id"])
    assert stale.status_code == 409
    assert not ChatMessage.objects.filter(author_kind="participant").exists()


def test_reset_recovers_a_stuck_room(rooms, project):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    ChatRoom.objects.filter(pk=room["id"]).update(
        pending_speakers=[1, 2, 3],
        turn_busy_until=timezone.now() + chat_runtime.timedelta(seconds=900),
    )
    cleared = advance(rooms, room["id"], reset=True)
    assert cleared.status_code == 200
    assert cleared.json()["room"]["pendingSpeakers"] == []
    assert ChatRoom.objects.get(pk=room["id"]).turn_busy_until is None


def test_posting_is_refused_while_a_turn_is_unfinished(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    stub(monkeypatch, [completion("First.")])
    assert post(rooms, room["id"], "Go").status_code == 201
    second = post(rooms, room["id"], "Again")
    assert second.status_code == 409
    assert "Finish or reset" in second.json()["detail"]


def test_a_room_without_unmuted_participants_cannot_start_a_turn(rooms, project):
    room = make_room(rooms, project)
    empty = post(rooms, room["id"], "Anyone there?")
    assert empty.status_code == 409
    assert "unmuted participant" in empty.json()["detail"]


def test_a_closed_room_refuses_posts_and_advances(rooms, project):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    rooms.delete(f"/api/v1/chat/rooms/{room['id']}")
    assert post(rooms, room["id"], "Hello").status_code == 409
    assert advance(rooms, room["id"]).status_code == 409


def test_prompt_carries_topic_persona_roster_and_the_shared_transcript(rooms, project, monkeypatch):
    room = make_room(rooms, project, topic="Choose a queue")
    add_participant(rooms, room["id"], "Architect", position=1, can_act=True)
    add_participant(rooms, room["id"], "Reviewer", position=2)
    requests = stub(monkeypatch, [completion("Use Redis."), completion("I disagree.")])
    post(rooms, room["id"], "Which queue?")
    advance(rooms, room["id"])
    advance(rooms, room["id"])

    first = requests[0][1]["json"]["messages"]
    system = first[0]
    assert system["role"] == "system"
    assert "Choose a queue" in system["content"]
    assert "Your role in this room: Architect" in system["content"]
    assert "You are the Architect." in system["content"]
    assert "- Architect (acts)" in system["content"]
    assert "- Reviewer (reads only)" in system["content"]
    assert "acting participant" in system["content"]
    # The human's line is a labelled user line, not a bare message.
    assert first[1] == {"role": "user", "content": "room-owner: Which queue?"}

    # The Reviewer sees the Architect's reply as another speaker's labelled line.
    second = requests[1][1]["json"]["messages"]
    assert "Your role in this room: Reviewer" in second[0]["content"]
    assert "read-only participant" in second[0]["content"]
    assert second[1]["role"] == "user"
    assert "room-owner: Which queue?" in second[1]["content"]
    assert "Architect: Use Redis." in second[1]["content"]
    assert all(item["role"] != "assistant" for item in second[1:])


def test_a_participant_sees_its_own_earlier_lines_as_its_own_turns(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    requests = stub(monkeypatch, [completion("First thought."), completion("Second thought.")])
    post(rooms, room["id"], "Go", rounds=2)
    advance(rooms, room["id"])
    advance(rooms, room["id"])
    second = requests[1][1]["json"]["messages"]
    assert {"role": "assistant", "content": "First thought."} in second
    assert any(item["role"] == "user" and "room-owner: Go" in item["content"] for item in second)


def test_the_prompt_window_drops_old_messages_but_history_keeps_everything(
    rooms, project, monkeypatch, settings
):
    settings.CHAT_ROOM_CONTEXT_MESSAGES = 3
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    requests = stub(monkeypatch, [completion(f"Reply {index}.") for index in range(6)])
    for index in range(5):
        assert post(rooms, room["id"], f"Question {index}").status_code == 201
        assert advance(rooms, room["id"]).json()["advanced"] is True
    windowed = requests[-1][1]["json"]["messages"]
    body = json.dumps(windowed)
    assert "Question 4" in body
    assert "Question 0" not in body, "the window must drop the oldest messages"
    # Nothing was pruned: ten messages remain readable.
    history = rooms.get(f"/api/v1/chat/rooms/{room['id']}/messages?latest=true&limit=50").json()
    assert history["messageCount"] == 10
    assert len(history["messages"]) == 10
    assert "Question 0" in json.dumps(history["messages"])


def test_history_pages_latest_before_and_after_with_a_capped_limit(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    stub(monkeypatch, [completion(f"Reply {index}.") for index in range(4)])
    for index in range(4):
        post(rooms, room["id"], f"Question {index}")
        advance(rooms, room["id"])
    latest = rooms.get(f"/api/v1/chat/rooms/{room['id']}/messages?latest=true&limit=3").json()
    assert [item["seq"] for item in latest["messages"]] == [6, 7, 8]
    assert latest["olderCursor"] == 6
    assert latest["hasMore"] is True
    older = rooms.get(
        f"/api/v1/chat/rooms/{room['id']}/messages?before={latest['olderCursor']}&limit=3"
    ).json()
    assert [item["seq"] for item in older["messages"]] == [3, 4, 5]
    oldest = rooms.get(f"/api/v1/chat/rooms/{room['id']}/messages?before=3&limit=10").json()
    assert [item["seq"] for item in oldest["messages"]] == [1, 2]
    assert oldest["hasMore"] is False
    assert oldest["olderCursor"] is None
    forward = rooms.get(f"/api/v1/chat/rooms/{room['id']}/messages?after=6&limit=10").json()
    assert [item["seq"] for item in forward["messages"]] == [7, 8]


def test_history_rejects_bad_cursors_and_mixed_modes(rooms, project):
    room = make_room(rooms, project)
    base = f"/api/v1/chat/rooms/{room['id']}/messages"
    assert rooms.get(f"{base}?before=bad").status_code == 400
    assert rooms.get(f"{base}?before=0").status_code == 400
    assert rooms.get(f"{base}?after=-1").status_code == 400
    assert rooms.get(f"{base}?limit=nope").status_code == 400
    assert rooms.get(f"{base}?after=1&before=4").status_code == 400
    assert rooms.get(f"{base}?after=1&latest=true").status_code == 400
    # An oversize limit is clamped rather than refused.
    assert rooms.get(f"{base}?latest=true&limit=5000").status_code == 200


def test_room_caps_refuse_the_next_turn_without_calling_the_provider(
    rooms, project, monkeypatch, settings
):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    requests = stub(monkeypatch, [completion("Hi.")])
    post(rooms, room["id"], "Go")
    advance(rooms, room["id"])
    assert len(requests) == 1
    settings.CHAT_ROOM_MAX_MESSAGES = 2
    refused = post(rooms, room["id"], "More")
    assert refused.status_code == 409
    assert "message count limit (2)" in refused.json()["detail"]
    assert "preserved" in refused.json()["detail"]
    assert len(requests) == 1, "a refused turn must not reach the provider"
    assert ChatRoom.objects.get(pk=room["id"]).message_count == 2


def test_open_room_cap_per_user(rooms, project, settings):
    settings.CHAT_ROOM_MAX_OPEN_ROOMS_PER_USER = 1
    make_room(rooms, project, name="First")
    second = rooms.post(
        "/api/v1/chat/rooms",
        {"project": project.slug, "name": "Second"},
        content_type="application/json",
    )
    assert second.status_code == 409
    assert "open rooms per user limit (1)" in second.json()["detail"]


def test_duplicate_room_name_per_project_is_refused(rooms, project):
    make_room(rooms, project, name="Only one")
    duplicate = rooms.post(
        "/api/v1/chat/rooms",
        {"project": project.slug, "name": "Only one"},
        content_type="application/json",
    )
    assert duplicate.status_code == 400
    other = Project.objects.create(slug="second", name="Second")
    assert make_room(rooms, other, name="Only one")["name"] == "Only one"


def test_truncated_completion_keeps_partial_text_and_reports_502(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    stub(monkeypatch, [completion("Half a thou", finish_reason="length")])
    post(rooms, room["id"], "Go")
    truncated = advance(rooms, room["id"])
    assert truncated.status_code == 502
    assert "output token limit" in truncated.json()["detail"]
    saved = ChatMessage.objects.get(author_kind="participant")
    assert saved.text == "Half a thou"


def test_invalid_and_empty_completions_are_refused(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    stub(
        monkeypatch,
        [
            completion(""),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {}}]}),
        ],
    )
    post(rooms, room["id"], "Go")
    assert advance(rooms, room["id"]).status_code == 502
    assert advance(rooms, room["id"]).status_code == 502
    assert not ChatMessage.objects.filter(author_kind="participant").exists()


def test_duplicate_tool_call_ids_are_refused(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect", can_act=True)
    stub(
        monkeypatch,
        [
            completion(
                calls=[
                    tool_call("get_task", {"id": 1}, call_id="same"),
                    tool_call("get_task", {"id": 2}, call_id="same"),
                ]
            )
        ],
    )
    post(rooms, room["id"], "Go")
    assert advance(rooms, room["id"]).status_code == 502


def test_unconfigured_and_unreachable_provider_surface_distinctly(
    rooms, project, monkeypatch, settings
):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    post(rooms, room["id"], "Go")
    settings.FIREWORKS_API_KEY = ""
    missing = advance(rooms, room["id"])
    assert missing.status_code == 503
    assert "not configured" in missing.json()["detail"]
    settings.FIREWORKS_API_KEY = "fireworks-server-secret"
    calls = []

    def unreachable(method, url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", unreachable)
    down = advance(rooms, room["id"])
    assert down.status_code == 502
    assert "no automatic retry" in down.json()["detail"]
    assert len(calls) == 1, "a transport failure must not be retried"


def test_provider_key_never_reaches_the_client(rooms, project, monkeypatch, settings):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    requests = stub(monkeypatch, [completion("Fine.")])
    post(rooms, room["id"], "Go")
    response = advance(rooms, room["id"])
    assert settings.FIREWORKS_API_KEY not in str(response.content)
    url, kwargs = requests[0]
    assert url == "https://api.fireworks.ai/inference/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == f"Bearer {settings.FIREWORKS_API_KEY}"
    assert kwargs["json"]["model"] == MODEL
    assert kwargs["json"]["reasoning_effort"] == "none"


def test_each_participant_can_run_a_different_model(rooms, project, monkeypatch):
    other = "accounts/fireworks/models/other-model"
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect", position=1)
    assert (
        add_participant(
            rooms,
            room["id"],
            "Reviewer",
            position=2,
            model={"providerID": "fireworks", "modelID": other},
        ).status_code
        == 201
    )
    requests = stub(monkeypatch, [completion("A."), completion("B.")])
    post(rooms, room["id"], "Go")
    advance(rooms, room["id"])
    advance(rooms, room["id"])
    assert [request[1]["json"]["model"] for request in requests] == [MODEL, other]
    stored = list(
        ChatMessage.objects.filter(author_kind="participant")
        .order_by("seq")
        .values_list("model", flat=True)
    )
    assert stored == [
        {"providerID": "fireworks", "modelID": MODEL},
        {"providerID": "fireworks", "modelID": other},
    ]


def test_worker_participants_are_refused_rather_than_faked(rooms, project, monkeypatch):
    agent = AgentModel(name="worker-agent")
    agent.set_new_api_key()
    agent.save()
    worker = Worker.objects.create(
        name="box",
        agent=agent,
        connected=True,
        stream_connected=True,
        last_sync_at=timezone.now(),
        model_catalog=[{"providerID": "huggingface", "modelID": "deepseek"}],
    )
    room = make_room(rooms, project)
    created = rooms.post(
        f"/api/v1/chat/rooms/{room['id']}/participants",
        {
            "role": "Builder",
            "kind": "worker",
            "worker": worker.name,
            "model": {"providerID": "huggingface", "modelID": "deepseek"},
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    requests = stub(monkeypatch, [completion("should never run")])
    post(rooms, room["id"], "Go")
    refused = advance(rooms, room["id"])
    assert refused.status_code == 501
    assert "not implemented yet" in refused.json()["detail"]
    assert not requests, "a worker participant must not fall back to the provider"
    assert not ChatMessage.objects.filter(author_kind="participant").exists()


def test_authorization_owner_writes_viewer_and_agent_read(rooms, project, agent_clients):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    anonymous = Client()
    assert anonymous.get("/api/v1/chat/rooms").status_code in {401, 403}
    assert anonymous.post("/api/v1/chat/rooms").status_code in {401, 403}

    agent_client = agent_clients[0]
    assert agent_client.get("/api/v1/chat/rooms").status_code == 200
    assert agent_client.get(f"/api/v1/chat/rooms/{room['id']}").status_code == 200
    assert agent_client.get(f"/api/v1/chat/rooms/{room['id']}/messages").status_code == 200
    assert post(agent_client, room["id"], "Let me in").status_code == 403
    assert advance(agent_client, room["id"]).status_code == 403

    viewer = get_user_model().objects.create_user("room-viewer", password="test-password")
    UserProfile.objects.update_or_create(user=viewer, defaults={"role": "viewer"})
    viewer_client = Client()
    viewer_client.force_login(viewer)
    assert viewer_client.get(f"/api/v1/chat/rooms/{room['id']}").status_code == 200
    assert post(viewer_client, room["id"], "Hello").status_code == 403

    member = get_user_model().objects.create_user("room-member", password="test-password")
    UserProfile.objects.update_or_create(user=member, defaults={"role": "member"})
    member_client = Client()
    member_client.force_login(member)
    assert post(member_client, room["id"], "Hello").status_code == 403
    assert member_client.get(f"/api/v1/chat/rooms/{room['id']}").status_code == 200


def test_room_reads_are_never_cached(rooms, project):
    room = make_room(rooms, project)
    response = rooms.get(f"/api/v1/chat/rooms/{room['id']}")
    assert response["Cache-Control"] == "no-store, private"


def test_models_endpoint_lists_only_the_allowlist(rooms, settings):
    response = rooms.get("/api/v1/chat/models")
    assert response.status_code == 200
    assert [item["modelID"] for item in response.json()["models"]] == list(
        settings.CHAT_ROOM_MODELS
    )
    assert all(item["providerID"] == "fireworks" for item in response.json()["models"])
    assert response.json()["limits"]["max_participants"] == settings.CHAT_ROOM_MAX_PARTICIPANTS


def test_api_alias_serves_the_same_rooms(rooms, project):
    room = make_room(rooms, project)
    assert rooms.get(f"/api/chat/rooms/{room['id']}").status_code == 200


def test_removing_the_speaking_participant_is_refused(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    stub(monkeypatch, [completion("Hi.")])
    post(rooms, room["id"], "Go")
    speaking = ChatParticipant.objects.get(role="Architect")
    blocked = rooms.delete(f"/api/v1/chat/rooms/{room['id']}/participants/{speaking.pk}")
    assert blocked.status_code == 409
    assert "speaking" in blocked.json()["detail"]


def test_removed_participant_keeps_its_name_on_past_messages(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect", position=1)
    add_participant(rooms, room["id"], "Reviewer", position=2)
    stub(monkeypatch, [completion("Mine."), completion("Also mine.")])
    post(rooms, room["id"], "Go")
    advance(rooms, room["id"])
    advance(rooms, room["id"])
    architect = ChatParticipant.objects.get(role="Architect")
    assert (
        rooms.delete(f"/api/v1/chat/rooms/{room['id']}/participants/{architect.pk}").status_code
        == 204
    )
    message = ChatMessage.objects.get(text="Mine.")
    assert message.participant_id is None
    assert message.role_label == "Architect"


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks are only guaranteed on PostgreSQL"
)
def test_postgres_allows_only_one_inflight_speaker(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Architect")
    add_participant(rooms, room["id"], "Reviewer")
    stub(monkeypatch, [completion("One."), completion("Two.")])
    post(rooms, room["id"], "Go")
    barrier = threading.Barrier(2)
    results = []

    def contend():
        barrier.wait()
        try:
            instance = ChatRoom.objects.get(pk=room["id"])
            _, participant = chat_runtime.begin_turn(instance)
            results.append("started" if participant else "empty")
        except Exception as error:  # noqa: BLE001 - the refusal is the assertion
            results.append("busy" if "already in progress" in str(error) else repr(error))
        finally:
            connection.close()

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["busy", "started"]
