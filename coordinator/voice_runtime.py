"""Backend orchestration: Fireworks reasons; ElevenLabs handles STT and TTS."""

import json
import wave
from datetime import timedelta
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, permissions, serializers
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .dbretry import retry_sqlite
from .models import VoiceConversation
from .voice import VoiceTokenAuthentication, execute_tool, validate_session
from .voice_policy import PROMPT, tool_configs

ELEVENLABS_ORIGIN = "https://api.elevenlabs.io/v1"


def provider_request(provider, path, **kwargs):
    if provider != "Fireworks" or path != "chat/completions":
        raise ValueError("Only Fireworks chat completions are supported")
    key = settings.FIREWORKS_API_KEY
    origin = "https://api.fireworks.ai/inference/v1"
    headers = {"Authorization": f"Bearer {key}"}
    if not key:
        raise services.TaskError(f"{provider} is not configured", 503)
    try:
        response = httpx.request(
            "POST",
            f"{origin}/{path}",
            headers=headers,
            timeout=45,
            follow_redirects=False,
            **kwargs,
        )
    except httpx.HTTPError:
        raise services.TaskError(
            f"{provider} could not be reached; no automatic retry was made", 502
        ) from None
    if not response.is_success:
        raise services.TaskError(
            f"{provider} rejected the request (HTTP {response.status_code})", 502
        )
    return response


def response_json(response, provider):
    try:
        return response.json()
    except ValueError:
        raise services.TaskError(f"{provider} returned an invalid response", 502) from None


def speech_request(path, **kwargs):
    key = settings.ELEVENLABS_API_KEY
    if not key:
        raise services.TaskError("ElevenLabs speech is not configured", 503)
    headers = {"xi-api-key": key, **kwargs.pop("headers", {})}
    try:
        response = httpx.request(
            "POST",
            f"{ELEVENLABS_ORIGIN}/{path}",
            headers=headers,
            timeout=60,
            follow_redirects=False,
            **kwargs,
        )
    except httpx.HTTPError:
        raise services.TaskError(
            "ElevenLabs speech could not be reached; no automatic retry was made", 502
        ) from None
    if not response.is_success:
        raise services.TaskError(
            f"ElevenLabs rejected the speech request (HTTP {response.status_code})", 502
        )
    return response


@retry_sqlite
@transaction.atomic
def begin_turn(conversation):
    session = services.locked(VoiceConversation.objects.select_related("user", "auth_session")).get(
        pk=conversation.pk
    )
    validate_session(session)
    if session.busy_until and session.busy_until > timezone.now():
        raise services.TaskError("A PM reply is already in progress", 409)
    if len(session.messages) > 100 or len(json.dumps(session.messages)) > 120000:
        raise services.TaskError("This conversation is full; clear it to start another", 409)
    session.busy_until = timezone.now() + timedelta(minutes=5)
    session.save(update_fields=["busy_until"])
    return session


def reason(session, text):
    messages = [*session.messages, {"role": "user", "content": text}]
    events = []
    for _ in range(5):
        if len(json.dumps(messages)) > 120000:
            raise services.TaskError("This conversation is full; clear it to start another", 409)
        data = response_json(
            provider_request(
                "Fireworks",
                "chat/completions",
                json={
                    "model": settings.FIREWORKS_MODEL,
                    "messages": [{"role": "system", "content": PROMPT}, *messages],
                    "tools": tool_configs(),
                    "tool_choice": "auto",
                    "temperature": 0.2,
                    "max_tokens": 2048,
                },
            ),
            "Fireworks",
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            content = message.get("content") or ""
            if not isinstance(content, str) or not isinstance(calls, list) or len(calls) > 8:
                raise ValueError
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ValueError
            parsed = []
            for call in calls:
                name = call["function"]["name"]
                args = json.loads(call["function"]["arguments"])
                if (
                    not isinstance(args, dict)
                    or "conversation_id" in args
                    or not isinstance(call["id"], str)
                    or not isinstance(name, str)
                ):
                    raise ValueError
                parsed.append((call["id"], name, args))
            if len({item[0] for item in parsed}) != len(parsed):
                raise ValueError
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            raise services.TaskError(
                "Fireworks returned an invalid or incomplete reply", 502
            ) from None
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
                for call_id, name, args in parsed
            ]
        messages.append(assistant)
        if not calls:
            if not content.strip():
                raise services.TaskError("Fireworks returned an empty reply", 502)
            return messages, content, events
        for call_id, name, args in parsed:
            try:
                result = execute_tool(
                    session,
                    name,
                    {
                        **args,
                        "conversation_id": f"conv_{session.pk.hex}",
                    },
                )
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
        # Keep confirmed tool outcomes even if the next provider completion fails.
        VoiceConversation.objects.filter(
            pk=session.pk,
            busy_until=session.busy_until,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).update(messages=messages)
    raise services.TaskError("PM tool-call limit reached; check task state before continuing", 502)


class VoiceRuntimeView(APIView):
    authentication_classes = [VoiceTokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response


class ChatInput(serializers.Serializer):
    text = serializers.CharField(max_length=4000)


class ToolEvent(serializers.Serializer):
    tool = serializers.CharField()
    is_error = serializers.BooleanField()


class ChatOutput(serializers.Serializer):
    text = serializers.CharField()
    events = ToolEvent(many=True)


class VoiceChatView(VoiceRuntimeView):
    @extend_schema(request=ChatInput, responses=ChatOutput)
    def post(self, request):
        serializer = ChatInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = begin_turn(request.auth)
        try:
            messages, text, events = reason(session, serializer.validated_data["text"])
            fresh = (
                VoiceConversation.objects.select_related("user", "auth_session")
                .filter(pk=session.pk)
                .first()
            )
            if fresh is None:
                raise exceptions.AuthenticationFailed(
                    "Voice conversation access expired; reconnect"
                )
            validate_session(fresh)
            VoiceConversation.objects.filter(
                pk=session.pk,
                busy_until=session.busy_until,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).update(messages=messages)
            return Response({"text": text, "events": events})
        finally:
            VoiceConversation.objects.filter(pk=session.pk, busy_until=session.busy_until).update(
                busy_until=None
            )


class TranscriptionInput(serializers.Serializer):
    file = serializers.FileField(
        help_text="Mono 24 kHz PCM16 WAV audio, at most 30 seconds / 4 MiB"
    )


class TranscriptionOutput(serializers.Serializer):
    text = serializers.CharField()


def transcribe(audio):
    if not settings.ELEVENLABS_STT_MODEL:
        raise services.TaskError("ElevenLabs transcription is not configured", 503)
    # Only the bounded clip and the speech model go upstream; never the owner's
    # token, cookies, prompts or tools.
    data = response_json(
        speech_request(
            "speech-to-text",
            files={"file": ("recording.wav", audio, "audio/wav")},
            data={"model_id": settings.ELEVENLABS_STT_MODEL},
        ),
        "ElevenLabs",
    )
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or len(text) > 4000:
        raise services.TaskError("ElevenLabs returned an invalid transcript", 502)
    return text


class VoiceTranscriptionView(VoiceRuntimeView):
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=TranscriptionInput, responses=TranscriptionOutput)
    def post(self, request):
        serializer = TranscriptionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        audio = serializer.validated_data["file"]
        if audio.size > 4 * 1024 * 1024:
            raise services.TaskError("Audio clip must be at most 4 MiB", 413)
        if audio.content_type not in {"audio/wav", "audio/x-wav"}:
            raise serializers.ValidationError("Expected mono 24 kHz PCM16 WAV audio")
        try:
            with wave.open(audio, "rb") as recording:
                frames = recording.getnframes()
                if (
                    recording.getnchannels() != 1
                    or recording.getsampwidth() != 2
                    or recording.getframerate() != 24000
                    or recording.getcomptype() != "NONE"
                    or not 0 < frames <= 24000 * 30
                ):
                    raise ValueError
                pcm = recording.readframes(frames)
                if len(pcm) != frames * 2:
                    raise ValueError
        except (wave.Error, EOFError, ValueError):
            raise serializers.ValidationError(
                "Expected a valid mono 24 kHz PCM16 WAV clip, at most 30 seconds"
            ) from None
        audio.seek(0)
        return Response({"text": transcribe(audio.read())})


class SpeechInput(serializers.Serializer):
    text = serializers.CharField(max_length=2000)

    def validate_text(self, value):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise serializers.ValidationError("Speech text must be valid Unicode") from None
        return value


class VoiceSpeechView(VoiceRuntimeView):
    @extend_schema(request=SpeechInput, responses={(200, "audio/mpeg"): OpenApiTypes.BINARY})
    def post(self, request):
        serializer = SpeechInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not settings.ELEVENLABS_API_KEY or not settings.ELEVENLABS_VOICE_ID:
            raise services.TaskError("ElevenLabs speech synthesis is not configured", 503)
        response = speech_request(
            f"text-to-speech/{quote(settings.ELEVENLABS_VOICE_ID, safe='')}",
            json={
                "text": serializer.validated_data["text"],
                "model_id": settings.ELEVENLABS_TTS_MODEL,
            },
            headers={"Accept": "audio/mpeg"},
        )
        audio = response.content
        if (
            response.headers.get("content-type", "").split(";")[0] != "audio/mpeg"
            or not audio
            or len(audio) > 8 * 1024 * 1024
        ):
            raise services.TaskError("ElevenLabs returned invalid speech audio", 502)
        return HttpResponse(audio, content_type="audio/mpeg")
