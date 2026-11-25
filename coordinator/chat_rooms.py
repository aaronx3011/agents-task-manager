"""Chat room REST surface, tool authorization and tool dispatch (GH #149).

Authorization: owners mutate, agent keys and viewers read. A room participant's
tool call is authorized against ``ChatParticipant.can_act`` re-read from the
locked row, never against the filtered schema list alone, so a read-only
participant that fabricates a mutating call is refused server-side.

Mutating tools are deliberately limited to the set the PM voice agent already
exposes and are executed through its ``validated_tool``/``run_tool`` path, so
every write stays serializer-revalidated, fingerprinted and audited as the room
owner rather than as the model.
"""

import hashlib
import json

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import authentication, exceptions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import UserTokenAuthentication

from . import chat_policy, chat_runtime, services
from .auth import AgentKeyAuthentication, AgentOrViewerReads, IsOwner
from .dbretry import retry_sqlite
from .models import (
    ChatParticipant,
    ChatRoom,
    ChatToolResult,
    Project,
    WorkerSession,
)
from .voice import run_tool, validated_tool


def room_card(room, detail=False):
    card = {
        "id": room.pk,
        "project": room.project.slug,
        "name": room.name,
        "topic": room.topic,
        "status": room.status,
        "roundsPerTurn": room.rounds_per_turn,
        "createdBy": room.created_by.get_username(),
        "turn": room.turn_seq,
        "turnBusy": bool(room.turn_busy_until and room.turn_busy_until > timezone.now()),
        "pendingSpeakers": list(room.pending_speakers),
        "messageCount": room.message_count,
        "historyBytes": room.history_bytes,
        "createdAt": room.created_at.isoformat(),
        "updatedAt": room.updated_at.isoformat(),
    }
    if detail:
        card["participants"] = [
            participant_card(item) for item in room.participants.order_by("position", "pk")
        ]
        card["limits"] = chat_runtime.limits()
    return card


def participant_card(participant):
    return {
        "id": participant.pk,
        "position": participant.position,
        "role": participant.role,
        "persona": participant.persona,
        "kind": participant.kind,
        "model": participant.model,
        "worker": participant.worker.name if participant.worker_id else None,
        "agent": participant.agent,
        "canAct": participant.can_act,
        "isMuted": participant.is_muted,
    }


def message_card(message):
    return {
        "id": message.pk,
        "seq": message.seq,
        "turn": message.turn,
        "authorKind": message.author_kind,
        "participantId": message.participant_id,
        "role": message.role_label,
        "text": message.text,
        "toolEvents": message.tool_events,
        "model": message.model,
        "createdAt": message.created_at.isoformat(),
    }


class ChatModelChoice(serializers.Serializer):
    providerID = serializers.CharField(max_length=100)
    modelID = serializers.CharField(max_length=200)


class RoomInput(serializers.Serializer):
    project = serializers.SlugField()
    name = serializers.CharField(max_length=120)
    topic = serializers.CharField(max_length=20000, allow_blank=True, required=False, default="")
    roundsPerTurn = serializers.IntegerField(min_value=1, required=False)


class RoomPatchInput(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    topic = serializers.CharField(max_length=20000, allow_blank=True, required=False)
    status = serializers.ChoiceField(choices=ChatRoom.Status.choices, required=False)
    roundsPerTurn = serializers.IntegerField(min_value=1, required=False)


class ParticipantInput(serializers.Serializer):
    role = serializers.CharField(max_length=60)
    persona = serializers.CharField(max_length=20000, allow_blank=True, required=False, default="")
    kind = serializers.ChoiceField(
        choices=ChatParticipant.Kind.choices, required=False, default=ChatParticipant.Kind.PROVIDER
    )
    model = ChatModelChoice(required=False)
    worker = serializers.CharField(max_length=100, required=False, allow_blank=True)
    agent = serializers.CharField(max_length=100, required=False, allow_blank=True)
    canAct = serializers.BooleanField(required=False, default=False)
    isMuted = serializers.BooleanField(required=False, default=False)
    position = serializers.IntegerField(min_value=1, required=False)


class ParticipantPatchInput(serializers.Serializer):
    role = serializers.CharField(max_length=60, required=False)
    persona = serializers.CharField(max_length=20000, allow_blank=True, required=False)
    model = ChatModelChoice(required=False)
    agent = serializers.CharField(max_length=100, required=False, allow_blank=True)
    canAct = serializers.BooleanField(required=False)
    isMuted = serializers.BooleanField(required=False)
    position = serializers.IntegerField(min_value=1, required=False)


class PostMessageInput(serializers.Serializer):
    text = serializers.CharField(max_length=20000)
    rounds = serializers.IntegerField(min_value=1, required=False)


class AdvanceInput(serializers.Serializer):
    rounds = serializers.IntegerField(min_value=1, required=False)
    reset = serializers.BooleanField(required=False, default=False)


class RoomView(APIView):
    """Base: owners write, agent keys and signed-in viewers read."""

    # Project default order: a Bearer agent key, a mobile device token, then the
    # CSRF-checked browser session. Session auth stays last so writes from a
    # browser are still CSRF-enforced.
    authentication_classes = [
        AgentKeyAuthentication,
        UserTokenAuthentication,
        authentication.SessionAuthentication,
    ]

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [AgentOrViewerReads()]
        return [IsOwner()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response


def room_queryset():
    return ChatRoom.objects.select_related("project", "created_by")


def get_room(pk):
    return get_object_or_404(room_queryset(), pk=pk)


def resolve_model(payload, kind, worker):
    """Validate a participant's model for its kind, before anything is written."""
    model = payload.get("model")
    if model is None:
        raise serializers.ValidationError({"model": "Choose a provider and model"})
    model = services._validate_model(dict(model))
    if kind == ChatParticipant.Kind.PROVIDER:
        if not chat_policy.model_allowed(model):
            allowed = ", ".join(settings.CHAT_ROOM_MODELS) or "(none configured)"
            raise serializers.ValidationError(
                {"model": f"Provider participants must use fireworks with one of: {allowed}"}
            )
        return model
    if not services._model_allowed(worker, model):
        raise serializers.ValidationError({"model": "Model is not in this worker's catalog"})
    return model


def resolve_worker(payload, kind):
    if kind != ChatParticipant.Kind.WORKER:
        return None
    name = (payload.get("worker") or "").strip()
    if not name:
        raise serializers.ValidationError({"worker": "Worker participants need a worker"})
    worker = services._resolve_worker(name)
    services._require_ready_worker(worker, "this chat room participant")
    return worker


class RoomsView(RoomView):
    @extend_schema(
        operation_id="chat_rooms_list",
        parameters=[
            OpenApiParameter("project", str, description="Registered project slug."),
            OpenApiParameter("status", str, description="open or closed."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        rooms = room_queryset()
        slug = request.query_params.get("project")
        if slug:
            rooms = rooms.filter(project__slug=slug)
        status = request.query_params.get("status")
        if status:
            rooms = rooms.filter(status=status)
        rows = list(rooms[: settings.CHAT_ROOM_PAGE_LIMIT])
        return Response({"count": rooms.count(), "items": [room_card(room) for room in rows]})

    @extend_schema(
        operation_id="chat_rooms_create", request=RoomInput, responses=OpenApiTypes.OBJECT
    )
    @retry_sqlite
    @transaction.atomic
    def post(self, request):
        data = RoomInput(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        project = get_object_or_404(Project, slug=payload["project"])
        open_rooms = ChatRoom.objects.filter(
            created_by=request.user, status=ChatRoom.Status.OPEN
        ).count()
        if open_rooms >= settings.CHAT_ROOM_MAX_OPEN_ROOMS_PER_USER:
            chat_runtime.full("open rooms per user", settings.CHAT_ROOM_MAX_OPEN_ROOMS_PER_USER)
        if ChatRoom.objects.filter(project=project, name=payload["name"]).exists():
            raise serializers.ValidationError(
                {"name": "This project already has a room by that name"}
            )
        rounds = min(payload.get("roundsPerTurn") or 1, settings.CHAT_ROOM_MAX_ROUNDS)
        room = ChatRoom.objects.create(
            project=project,
            name=payload["name"],
            topic=payload.get("topic", ""),
            rounds_per_turn=rounds,
            created_by=request.user,
        )
        return Response(room_card(room, detail=True), status=201)


class RoomDetailView(RoomView):
    @extend_schema(operation_id="chat_room_retrieve", responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        return Response(room_card(get_room(pk), detail=True))

    @extend_schema(
        operation_id="chat_room_update", request=RoomPatchInput, responses=OpenApiTypes.OBJECT
    )
    @retry_sqlite
    @transaction.atomic
    def patch(self, request, pk):
        data = RoomPatchInput(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        if not payload:
            raise serializers.ValidationError({"detail": "Pass at least one field to update"})
        room = services.locked(room_queryset().filter(pk=pk)).first()
        if room is None:
            return Response({"detail": "Not found."}, status=404)
        fields = []
        for name, column in (
            ("name", "name"),
            ("topic", "topic"),
            ("status", "status"),
        ):
            if name in payload:
                setattr(room, column, payload[name])
                fields.append(column)
        if "roundsPerTurn" in payload:
            room.rounds_per_turn = min(payload["roundsPerTurn"], settings.CHAT_ROOM_MAX_ROUNDS)
            fields.append("rounds_per_turn")
        if "name" in payload and (
            ChatRoom.objects.filter(project=room.project, name=room.name)
            .exclude(pk=room.pk)
            .exists()
        ):
            raise serializers.ValidationError(
                {"name": "This project already has a room by that name"}
            )
        room.save(update_fields=[*fields, "updated_at"])
        return Response(room_card(room, detail=True))

    @extend_schema(operation_id="chat_room_close", responses=OpenApiTypes.OBJECT)
    @retry_sqlite
    @transaction.atomic
    def delete(self, request, pk):
        """Close the room. History is retained; hard deletion is admin-only."""
        room = services.locked(room_queryset().filter(pk=pk)).first()
        if room is None:
            return Response({"detail": "Not found."}, status=404)
        room.status = ChatRoom.Status.CLOSED
        room.pending_speakers = []
        room.turn_busy_until = None
        room.save(update_fields=["status", "pending_speakers", "turn_busy_until", "updated_at"])
        return Response(room_card(room, detail=True))


class ParticipantsView(RoomView):
    @extend_schema(
        operation_id="chat_participant_create",
        request=ParticipantInput,
        responses=OpenApiTypes.OBJECT,
    )
    @retry_sqlite
    @transaction.atomic
    def post(self, request, pk):
        data = ParticipantInput(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        room = services.locked(room_queryset().filter(pk=pk)).first()
        if room is None:
            return Response({"detail": "Not found."}, status=404)
        existing = list(room.participants.order_by("position", "pk"))
        if len(existing) >= settings.CHAT_ROOM_MAX_PARTICIPANTS:
            chat_runtime.full("participants", settings.CHAT_ROOM_MAX_PARTICIPANTS)
        if any(item.role == payload["role"] for item in existing):
            raise serializers.ValidationError({"role": "This room already has that role"})
        kind = payload["kind"]
        worker = resolve_worker(payload, kind)
        model = resolve_model(payload, kind, worker)
        position = payload.get("position") or (existing[-1].position + 1 if existing else 1)
        if any(item.position == position for item in existing):
            raise serializers.ValidationError({"position": "That position is taken"})
        if payload["canAct"]:
            room.participants.filter(can_act=True).update(can_act=False)
        participant = ChatParticipant.objects.create(
            room=room,
            position=position,
            role=payload["role"],
            persona=payload.get("persona", ""),
            kind=kind,
            model=model,
            worker=worker,
            agent=payload.get("agent", ""),
            can_act=payload["canAct"],
            is_muted=payload["isMuted"],
        )
        return Response(participant_card(participant), status=201)


class ParticipantDetailView(RoomView):
    @extend_schema(
        operation_id="chat_participant_update",
        request=ParticipantPatchInput,
        responses=OpenApiTypes.OBJECT,
    )
    @retry_sqlite
    @transaction.atomic
    def patch(self, request, pk, participant_id):
        data = ParticipantPatchInput(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        if not payload:
            raise serializers.ValidationError({"detail": "Pass at least one field to update"})
        room = services.locked(room_queryset().filter(pk=pk)).first()
        if room is None:
            return Response({"detail": "Not found."}, status=404)
        participant = room.participants.filter(pk=participant_id).first()
        if participant is None:
            return Response({"detail": "Not found."}, status=404)
        fields = []
        if "role" in payload:
            if room.participants.filter(role=payload["role"]).exclude(pk=participant.pk).exists():
                raise serializers.ValidationError({"role": "This room already has that role"})
            participant.role = payload["role"]
            fields.append("role")
        if "persona" in payload:
            participant.persona = payload["persona"]
            fields.append("persona")
        if "agent" in payload:
            participant.agent = payload["agent"]
            fields.append("agent")
        if "isMuted" in payload:
            participant.is_muted = payload["isMuted"]
            fields.append("is_muted")
        if "model" in payload:
            participant.model = resolve_model(payload, participant.kind, participant.worker)
            fields.append("model")
        if "position" in payload and payload["position"] != participant.position:
            target = payload["position"]
            if room.participants.filter(position=target).exclude(pk=participant.pk).exists():
                # Two-step swap: clear the occupant's position first so the unique
                # constraint is never violated mid-update.
                other = room.participants.get(position=target)
                other.position = participant.position
                participant.position = 0
                participant.save(update_fields=["position"])
                other.save(update_fields=["position"])
            participant.position = target
            fields.append("position")
        if "canAct" in payload:
            if payload["canAct"]:
                # Clear the previous actor first: at most one per room.
                room.participants.filter(can_act=True).exclude(pk=participant.pk).update(
                    can_act=False
                )
            participant.can_act = payload["canAct"]
            fields.append("can_act")
        participant.save(update_fields=[*fields, "updated_at"])
        return Response(participant_card(participant))

    @extend_schema(operation_id="chat_participant_delete", responses=OpenApiTypes.OBJECT)
    @retry_sqlite
    @transaction.atomic
    def delete(self, request, pk, participant_id):
        room = services.locked(room_queryset().filter(pk=pk)).first()
        if room is None:
            return Response({"detail": "Not found."}, status=404)
        participant = room.participants.filter(pk=participant_id).first()
        if participant is None:
            return Response({"detail": "Not found."}, status=404)
        if room.pending_speakers and room.pending_speakers[0] == participant.pk:
            raise services.TaskError("That participant is speaking; finish the turn first", 409)
        room.pending_speakers = [item for item in room.pending_speakers if item != participant.pk]
        room.save(update_fields=["pending_speakers", "updated_at"])
        participant.delete()
        return Response(status=204)


class MessagesView(RoomView):
    @extend_schema(
        operation_id="chat_messages_list",
        parameters=[
            OpenApiParameter("latest", bool, description="Newest page, oldest-first."),
            OpenApiParameter("before", int, description="Exclusive older-page cursor."),
            OpenApiParameter("after", int, description="Exclusive newer-page cursor."),
            OpenApiParameter("limit", int, description="Page size, capped."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request, pk):
        room = get_room(pk)
        params = request.query_params
        latest = params.get("latest") in {"1", "true", "yes"}
        before, after = params.get("before"), params.get("after")
        if after is not None and (latest or before is not None):
            raise services.TaskError("Combine after with neither latest nor before", 400)
        try:
            limit = min(max(int(params.get("limit", 20)), 1), settings.CHAT_ROOM_PAGE_LIMIT)
            before = int(before) if before is not None else None
            after = int(after) if after is not None else None
            if (before is not None and before < 1) or (after is not None and after < 0):
                raise ValueError
        except (TypeError, ValueError):
            raise services.TaskError("Invalid history limit or cursor", 400) from None
        rows = room.messages.all()
        if after is not None:
            page = list(rows.filter(seq__gt=after).order_by("seq")[:limit])
        else:
            if before is not None:
                rows = rows.filter(seq__lt=before)
            page = list(reversed(list(rows.order_by("-seq")[:limit])))
        oldest = page[0].seq if page else 0
        return Response(
            {
                "messages": [message_card(message) for message in page],
                "cursor": page[-1].seq if page else 0,
                "olderCursor": oldest if page and oldest > 1 else None,
                "hasMore": bool(page) and oldest > 1,
                "messageCount": room.message_count,
            }
        )

    @extend_schema(
        operation_id="chat_messages_create",
        request=PostMessageInput,
        responses=OpenApiTypes.OBJECT,
    )
    def post(self, request, pk):
        data = PostMessageInput(data=request.data)
        data.is_valid(raise_exception=True)
        room = get_room(pk)
        room, message = chat_runtime.post_message(
            room, request.user, data.validated_data["text"], data.validated_data.get("rounds")
        )
        return Response({"message": message_card(message), "room": room_card(room)}, status=201)


class AdvanceView(RoomView):
    @extend_schema(
        operation_id="chat_room_advance", request=AdvanceInput, responses=OpenApiTypes.OBJECT
    )
    def post(self, request, pk):
        data = AdvanceInput(data=request.data)
        data.is_valid(raise_exception=True)
        payload = data.validated_data
        room = get_room(pk)
        room, message = chat_runtime.advance(
            room,
            lambda participant, tool, args: execute_chat_tool(room, participant, tool, args),
            rounds=payload.get("rounds"),
            reset=payload["reset"],
        )
        room.refresh_from_db()
        return Response(
            {
                "advanced": message is not None,
                "message": message_card(message) if message else None,
                "room": room_card(room),
            }
        )


class ChatModelsView(RoomView):
    @extend_schema(operation_id="chat_models_list", responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response(
            {
                "models": [
                    {"providerID": "fireworks", "modelID": name}
                    for name in settings.CHAT_ROOM_MODELS
                ],
                "limits": chat_runtime.limits(),
            }
        )


def session_for(args):
    worker = services._resolve_worker(args["worker"])
    return get_object_or_404(
        WorkerSession.objects.select_related("worker", "parent"),
        worker=worker,
        remote_id=args["session_id"],
    )


def run_session_tool(operation, args):
    """Read-only worker session reads, so a room can discuss stored sessions."""
    if operation == "list_sessions":
        sessions = WorkerSession.objects.select_related("worker", "parent").order_by(
            "-last_seen_at", "-pk"
        )
        if args.get("worker"):
            sessions = sessions.filter(worker=services._resolve_worker(args["worker"]))
        if args.get("directory"):
            sessions = sessions.filter(directory=args["directory"])
        limit = min(max(int(args.get("limit") or 10), 1), 20)
        rows = list(sessions[:limit])
        return {
            "count": sessions.count(),
            "items": [services.session_card(session) for session in rows],
        }
    if operation == "get_session":
        return services.session_card(session_for(args), detail=True)
    if operation == "read_session_messages":
        return services.recent_messages_read(
            session_for(args), before=args.get("before"), limit=args.get("limit") or 10
        )
    raise services.TaskError("Unknown chat room tool", 404)


def validated_session_tool(operation, data):
    allowed = chat_policy.SESSION_TOOL_SCHEMAS[operation][1].keys()
    unknown = set(data) - set(allowed)
    if unknown:
        raise serializers.ValidationError({"detail": "Unsupported tool parameters"})
    payload = dict(data)
    for name in ("worker", "session_id", "directory", "before"):
        if name in payload:
            payload[name] = serializers.CharField(max_length=200).run_validation(payload[name])
    if "limit" in payload:
        payload["limit"] = serializers.IntegerField(min_value=1, max_value=20).run_validation(
            payload["limit"]
        )
    for name in chat_policy.SESSION_REQUIRED[operation]:
        if not payload.get(name):
            raise serializers.ValidationError({name: "This field is required"})
    return payload


@retry_sqlite
@transaction.atomic
def execute_chat_tool(room, participant, operation, args):
    """Authorize and run one tool call for a room participant.

    The ``can_act`` check is re-read from the locked participant row: a filtered
    schema list is never treated as sufficient, so a read-only participant that
    fabricates a mutating call is refused here with nothing written.
    """
    current = services.locked(
        ChatRoom.objects.select_related("created_by").filter(pk=room.pk)
    ).first()
    if current is None:
        raise services.TaskError("This chat room no longer exists", 404)
    speaker = services.locked(ChatParticipant.objects.filter(pk=participant.pk)).first()
    if speaker is None or speaker.room_id != current.pk:
        raise services.TaskError("That participant is no longer in this room", 404)
    if operation not in chat_policy.allowed_tools(speaker):
        if operation in chat_policy.ACT_TOOLS:
            raise exceptions.PermissionDenied(
                "This participant is read-only; only the room's acting participant "
                "may change anything"
            )
        raise services.TaskError("Unknown chat room tool", 404)
    if operation in chat_policy.SESSION_TOOLS:
        validated = validated_session_tool(operation, args)
        result = json.loads(
            json.dumps(run_session_tool(operation, validated), cls=DjangoJSONEncoder)
        )
        return {"result": result, "replayed": False}
    validated = validated_tool(operation, args)
    fingerprint = hashlib.sha256(json.dumps([operation, args], sort_keys=True).encode()).hexdigest()
    if operation in chat_policy.ACT_TOOLS:
        previous = current.tool_results.filter(fingerprint=fingerprint).first()
        if previous:
            return {"result": previous.result, "replayed": True}
    # Audited as the room's owner, exactly as the voice PM agent is: an acting
    # participant mutates with its owner's authority, never with its own.
    result = json.loads(
        json.dumps(run_tool(operation, validated, current.created_by), cls=DjangoJSONEncoder)
    )
    ChatToolResult.objects.update_or_create(
        room=current,
        fingerprint=fingerprint,
        defaults={"tool": operation, "result": result, "participant": speaker},
    )
    return {"result": result, "replayed": False}
