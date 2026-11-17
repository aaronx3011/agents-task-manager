"""Clip-to-text adapter contract; no speech provider or API key required."""

import io
import wave

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from tests import test_voice

# Reuse the same authorized conversation setup as the PM tool/runtime tests.
owner = test_voice.owner
voice = test_voice.voice

pytestmark = pytest.mark.django_db


def wav(pcm=b"\x01\x00" * 12000, rate=24000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


def post(grant, content, mime="audio/wav"):
    return Client().post(
        "/api/v1/voice/transcriptions",
        {"file": SimpleUploadedFile("turn.wav", content, content_type=mime)},
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
    )


def elevenlabs_fixture(monkeypatch, response):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", request)
    return calls


def test_elevenlabs_receives_only_the_bounded_clip_and_model(voice, monkeypatch, settings):
    _, grant = voice
    calls = elevenlabs_fixture(
        monkeypatch,
        httpx.Response(200, json={"language_code": "eng", "text": "Check my tasks"}),
    )
    clip = wav()
    result = post(grant, clip)
    assert result.status_code == 200, result.content
    assert result.json() == {"text": "Check my tasks"}
    assert result["Cache-Control"] == "no-store, private"
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://api.elevenlabs.io/v1/speech-to-text")
    assert kwargs["headers"] == {"xi-api-key": settings.ELEVENLABS_API_KEY}
    assert kwargs["data"] == {"model_id": settings.ELEVENLABS_STT_MODEL}
    name, content, mime = kwargs["files"]["file"]
    assert (name, mime) == ("recording.wav", "audio/wav")
    assert content == clip
    assert kwargs["follow_redirects"] is False
    assert grant["tool_token"] not in repr(calls)
    assert settings.FIREWORKS_API_KEY not in repr(calls)
    assert "conversation" not in repr(kwargs["headers"]).lower()


@pytest.mark.parametrize(
    "clip,mime",
    [
        (b"webm", "audio/webm"),
        (b"not a wav", "audio/wav"),
        (wav(rate=44100), "audio/wav"),
        (wav(channels=2), "audio/wav"),
        (wav(width=1), "audio/wav"),
        (wav(b""), "audio/wav"),
        (wav()[:-100], "audio/wav"),
        (wav(b"\x00\x00" * (24000 * 31)), "audio/wav"),
    ],
)
def test_bad_or_oversize_duration_audio_never_reaches_upstream(voice, monkeypatch, clip, mime):
    _, grant = voice

    def unexpected(*args, **kwargs):
        pytest.fail("invalid recording reached speech provider")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", unexpected)
    assert post(grant, clip, mime).status_code == 400


def test_audio_payload_size_is_bounded(voice):
    _, grant = voice
    assert post(grant, b"x" * (4 * 1024 * 1024 + 1)).status_code == 413


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": {"message": "private upstream details"}}),
        httpx.Response(429, json={"detail": "private upstream details"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"text": 123}),
        httpx.Response(200, json=["private upstream details"]),
        httpx.Response(200, json={"text": "x" * 4001}),
    ],
)
def test_transcription_errors_are_generic(voice, monkeypatch, response):
    _, grant = voice
    elevenlabs_fixture(monkeypatch, response)
    result = post(grant, wav())
    assert result.status_code == 502
    assert b"private upstream details" not in result.content


def test_unreachable_provider_is_a_sanitized_error(voice, monkeypatch):
    _, grant = voice

    def request(*args, **kwargs):
        raise httpx.ConnectError("private upstream details")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", request)
    result = post(grant, wav())
    assert result.status_code == 502
    assert b"private upstream details" not in result.content


def test_silent_turn_is_not_a_fabricated_transcript(voice, monkeypatch):
    _, grant = voice
    elevenlabs_fixture(monkeypatch, httpx.Response(200, json={"text": ""}))
    assert post(grant, wav()).json() == {"text": ""}


def test_missing_elevenlabs_configuration_returns_service_unavailable(voice, settings):
    _, grant = voice
    settings.ELEVENLABS_API_KEY = ""
    assert post(grant, wav()).status_code == 503


def test_transcription_requires_active_owner_capability(voice):
    client, grant = voice
    assert Client().post("/api/v1/voice/transcriptions").status_code == 401
    assert client.delete(f"/api/v1/voice/sessions/{grant['id']}").status_code == 204
    assert post(grant, wav()).status_code == 401
