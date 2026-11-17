import hashlib
import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import authentication, exceptions, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Role
from accounts.services import role_of

from . import services
from .auth import IsOwner
from .dbretry import retry_sqlite
from .models import Project, ProjectPlan, Task, VoiceConversation, VoiceToolResult
from .queries import filter_tasks, task_queryset
from .serializers import (
    NoteSerializer,
    StatusUpdateSerializer,
    TaskDetailSerializer,
    TaskSerializer,
)
from .voice_policy import POLICY_VERSION, TOOL_SCHEMAS


def configured():
    return bool(settings.FIREWORKS_API_KEY and settings.FIREWORKS_MODEL)


def transcription_configured():
    return bool(settings.ELEVENLABS_API_KEY and settings.ELEVENLABS_STT_MODEL)


def tts_configured():
    return bool(
        settings.ELEVENLABS_API_KEY
        and settings.ELEVENLABS_VOICE_ID
        and settings.ELEVENLABS_TTS_MODEL
    )


class SessionInput(serializers.Serializer):
    text_only = serializers.BooleanField(default=False)


class SessionOutput(serializers.Serializer):
    id = serializers.UUIDField()
    tool_token = serializers.CharField()
    expires_at = serializers.DateTimeField()


class VoiceStatusOutput(serializers.Serializer):
    provider = serializers.CharField()
    configured = serializers.BooleanField()
    policy_version = serializers.CharField()
    speech_configured = serializers.BooleanField()
    speech_provider = serializers.CharField()
    transcription_configured = serializers.BooleanField()
    tts_configured = serializers.BooleanField()


class OwnerVoiceView(APIView):
    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [IsOwner]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response


class VoiceStatusView(OwnerVoiceView):
    @extend_schema(responses=VoiceStatusOutput)
    def get(self, request):
        return Response(
            {
                "provider": "fireworks",
                "configured": configured(),
                "speech_provider": "elevenlabs",
                "speech_configured": transcription_configured() and tts_configured(),
                "transcription_configured": transcription_configured(),
                "tts_configured": tts_configured(),
                "policy_version": POLICY_VERSION,
            }
        )


class VoiceSessionView(OwnerVoiceView):
    @extend_schema(request=SessionInput, responses={201: SessionOutput})
    def post(self, request):
        serializer = SessionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not configured():
            raise services.TaskError("Fireworks PM reasoning is not configured", 503)
        active = VoiceConversation.objects.filter(
            user=request.user, revoked_at=None, expires_at__gt=timezone.now()
        )
        if active.count() >= 3:
            raise services.TaskError("End an existing PM conversation before starting another", 429)
        token = secrets.token_urlsafe(32)
        session = VoiceConversation.objects.create(
            user=request.user,
            auth_session=Session.objects.get(pk=request.session.session_key),
            auth_hash=request.user.get_session_auth_hash(),
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        return Response(
            {
                "id": session.pk,
                "tool_token": token,
                "expires_at": session.expires_at,
            },
            status=201,
        )


class VoiceSessionDetailView(OwnerVoiceView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        session = get_object_or_404(VoiceConversation, pk=pk, user=request.user)
        return Response(
            {
                "active": session.revoked_at is None and session.expires_at > timezone.now(),
                "events": [
                    {"id": item.pk, "tool": item.tool, "created_at": item.created_at}
                    for item in session.tool_results.order_by("-id")[:30]
                ],
            }
        )

    @extend_schema(responses={204: None})
    def delete(self, request, pk):
        session = get_object_or_404(VoiceConversation, pk=pk, user=request.user)
        session.revoked_at = timezone.now()
        session.messages = []
        session.save(update_fields=["revoked_at", "messages"])
        return Response(status=204)


class VoiceTokenAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        token = request.headers.get("X-ATM-Voice-Token", "")
        if not 32 <= len(token) <= 128:
            raise exceptions.AuthenticationFailed("Invalid voice conversation access")
        session = (
            VoiceConversation.objects.select_related("user", "auth_session")
            .filter(token_hash=hashlib.sha256(token.encode()).hexdigest())
            .first()
        )
        if session is None:
            raise exceptions.AuthenticationFailed("Invalid voice conversation access")
        validate_session(session)
        return session.user, session

    def authenticate_header(self, request):
        return "VoiceSession"


class VoiceTokenScheme(OpenApiAuthenticationExtension):
    target_class = "coordinator.voice.VoiceTokenAuthentication"
    name = "voiceConversationToken"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "header", "name": "X-ATM-Voice-Token"}


def validate_session(session):
    user = session.user
    auth = session.auth_session.get_decoded()
    if (
        session.revoked_at
        or session.expires_at <= timezone.now()
        or session.auth_session.expire_date <= timezone.now()
        or not user.is_active
        or role_of(user) != Role.OWNER
        or auth.get("_auth_user_id") != str(user.pk)
        or not secrets.compare_digest(session.auth_hash, user.get_session_auth_hash())
    ):
        raise exceptions.AuthenticationFailed("Voice conversation access expired; reconnect")


class TaskListInput(serializers.Serializer):
    project = serializers.SlugField(required=False)
    status = serializers.ChoiceField(choices=Task.Status.values, required=False)
    q = serializers.CharField(max_length=300, required=False, allow_blank=True)
    limit = serializers.IntegerField(default=10, min_value=1, max_value=20)
    offset = serializers.IntegerField(default=0, min_value=0, max_value=10000)


def validated_tool(operation, data):
    if operation not in TOOL_SCHEMAS:
        raise services.TaskError("Unknown PM tool", 404)
    allowed = TOOL_SCHEMAS[operation][1].keys()
    unknown = set(data) - set(allowed) - {"conversation_id"}
    if unknown:
        raise serializers.ValidationError({"detail": "Unsupported tool parameters"})
    payload = {key: value for key, value in data.items() if key != "conversation_id"}
    if operation == "create_task":
        serializer = TaskSerializer(data=payload)
    elif operation == "list_tasks":
        serializer = TaskListInput(data=payload)
    elif operation == "update_task_status":
        serializer = StatusUpdateSerializer(data=payload)
    elif operation == "add_task_note":
        serializer = NoteSerializer(data=payload)
    else:
        serializer = None
    if "id" in allowed:
        task_id = serializers.IntegerField(min_value=1).run_validation(payload.get("id"))
        payload["id"] = task_id
    if operation == "get_project":
        payload["project"] = serializers.SlugField().run_validation(payload.get("project"))
    if serializer:
        serializer.is_valid(raise_exception=True)
        validated = dict(serializer.validated_data)
        if "id" in payload:
            validated["id"] = payload["id"]
        return validated
    return payload


def task_card(task):
    data = TaskSerializer(task_queryset().get(pk=task.pk)).data
    return {**data, "url": f"{settings.VOICE_PUBLIC_ORIGIN}/?task={task.pk}"}


def run_tool(operation, args, user):
    if operation == "list_projects":
        rows = []
        for project in Project.objects.order_by("slug"):
            counts = dict.fromkeys(Task.Status.values, 0)
            counts.update(
                {
                    item["status"]: item["count"]
                    for item in project.tasks.values("status").annotate(count=Count("id"))
                }
            )
            rows.append(
                {"slug": project.slug, "name": project.name, "kind": project.kind, "counts": counts}
            )
        return {"projects": rows}
    if operation == "get_project":
        project = get_object_or_404(Project, slug=args["project"])
        plan = ProjectPlan.objects.filter(project=project).first()
        return {
            "slug": project.slug,
            "name": project.name,
            "repo_url": project.repo_url,
            "docs_path": project.docs_path,
            "context": plan.context if plan else "",
            "instructions": plan.instructions if plan else "",
        }
    if operation == "list_tasks":
        tasks = filter_tasks(args)
        offset, limit = args["offset"], args["limit"]
        return {
            "count": tasks.count(),
            "offset": offset,
            "items": TaskSerializer(tasks[offset : offset + limit], many=True).data,
        }
    if operation == "get_task":
        task = get_object_or_404(task_queryset(), pk=args["id"])
        return {
            **TaskDetailSerializer(task).data,
            "url": f"{settings.VOICE_PUBLIC_ORIGIN}/?task={task.pk}",
        }
    actor = {"agent": None, "human_actor": user.get_username()}
    if operation == "create_task":
        args["depends_on"] = [task.pk for task in args.get("depends_on", [])]
        return task_card(services.create_task(**actor, **args))
    task_id = args.pop("id")
    if operation == "update_task_status":
        return task_card(services.update_status(task_id=task_id, **actor, **args))
    services.add_note(task_id=task_id, **actor, **args)
    return {"ok": True, "task_id": task_id}


@retry_sqlite
@transaction.atomic
def execute_tool(conversation, operation, body):
    # Serialize on the capability before both dedupe and task mutation. The
    # task/audit write and cached response commit together, including on PG.
    session = services.locked(VoiceConversation.objects.select_related("user", "auth_session")).get(
        pk=conversation.pk
    )
    validate_session(session)
    conversation_id = body.get("conversation_id")
    if (
        not isinstance(conversation_id, str)
        or not conversation_id.startswith("conv_")
        or len(conversation_id) > 128
    ):
        raise serializers.ValidationError({"conversation_id": "Expected provider conversation ID"})
    if session.provider_conversation and session.provider_conversation != conversation_id:
        raise exceptions.PermissionDenied("Voice access belongs to another conversation")
    if not session.provider_conversation:
        session.provider_conversation = conversation_id
        session.save(update_fields=["provider_conversation"])
    args = validated_tool(operation, body)
    canonical = {key: value for key, value in body.items() if key != "conversation_id"}
    fingerprint = hashlib.sha256(
        json.dumps([operation, canonical], sort_keys=True).encode()
    ).hexdigest()
    writes = {"create_task", "update_task_status", "add_task_note"}
    if operation in writes:
        previous = session.tool_results.filter(fingerprint=fingerprint).first()
        if previous:
            return {"result": previous.result, "replayed": True}
    result = json.loads(json.dumps(run_tool(operation, args, session.user), cls=DjangoJSONEncoder))
    # Reads refresh live state on each call; do not reuse cached status answers.
    VoiceToolResult.objects.update_or_create(
        conversation=session,
        fingerprint=fingerprint,
        defaults={"tool": operation, "result": result},
    )
    return {"result": result, "replayed": False}


class VoiceToolView(APIView):
    authentication_classes = [VoiceTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request, operation):
        if len(request.body) > 65536 or not isinstance(request.data, dict):
            raise serializers.ValidationError("Expected bounded JSON object")
        response = Response(execute_tool(request.auth, operation, request.data))
        response["Cache-Control"] = "no-store, private"
        return response
