"""Clip-to-realtime adapter contract; no speech provider or API key required."""

import base64
import io
import json
import wave

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


def socket_fixture(monkeypatch, events):
    sent, connections, closed = [], [], []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def send(self, value):
            sent.append(json.loads(value))

        def recv(self, timeout):
            assert 0 < timeout <= 60
            if not events:
                raise TimeoutError("private upstream details")
            value = events.pop(0)
            return value if isinstance(value, str) else json.dumps(value)

    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return Socket()

    monkeypatch.setattr("coordinator.voice_runtime.connect", connect)
    return sent, connections, closed


def test_local_parakeet_receives_only_pcm_and_transcription_commands(voice, monkeypatch, settings):
    _, grant = voice
    sent, connections, closed = socket_fixture(
        monkeypatch,
        [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Check my tasks",
            },
        ],
    )
    pcm = b"\x01\x00" * 12000
    result = post(grant, wav(pcm))
    assert result.status_code == 200, result.content
    assert result.json() == {"text": "Check my tasks"}
    assert result["Cache-Control"] == "no-store, private"
    assert connections[0][0] == "wss://voice.example/api/listen"
    assert connections[0][1]["origin"] == "https://voice.example"
    assert sent[0] == {
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "turn_detection": None,
        },
    }
    assert sent[-1] == {"type": "input_audio_buffer.commit"}
    assert all(item["type"] == "input_audio_buffer.append" for item in sent[1:-1])
    assert b"".join(base64.b64decode(item["audio"]) for item in sent[1:-1]) == pcm
    assert len(sent[1:-1]) == 3
    assert closed == [True]
    assert grant["tool_token"] not in str(connections) + str(sent)
    assert settings.FIREWORKS_API_KEY not in str(connections) + str(sent)


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

    monkeypatch.setattr("coordinator.voice_runtime.connect", unexpected)
    assert post(grant, clip, mime).status_code == 400


def test_audio_payload_size_is_bounded(voice):
    _, grant = voice
    assert post(grant, b"x" * (4 * 1024 * 1024 + 1)).status_code == 413


@pytest.mark.parametrize(
    "events",
    [
        ["invalid json"],
        [{"type": "error", "error": {"message": "private upstream details"}}],
        [{"type": "session.updated"}],  # disconnect/timeout after commit
        [
            {"type": "session.updated"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": 123},
        ],
    ],
)
def test_transcription_errors_are_generic_and_close_the_connection(voice, monkeypatch, events):
    _, grant = voice
    _, _, closed = socket_fixture(monkeypatch, events)
    result = post(grant, wav())
    assert result.status_code == 502
    assert b"private upstream details" not in result.content
    assert closed == [True]


def test_silent_turn_is_not_a_fabricated_transcript(voice, monkeypatch):
    _, grant = voice
    socket_fixture(
        monkeypatch,
        [
            {"type": "session.updated"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": ""},
        ],
    )
    assert post(grant, wav()).json() == {"text": ""}


def test_missing_local_configuration_returns_service_unavailable(voice, settings):
    _, grant = voice
    settings.LOCAL_SPEECH_ORIGIN = ""
    assert post(grant, wav()).status_code == 503


def test_transcription_requires_active_owner_capability(voice):
    client, grant = voice
    assert Client().post("/api/v1/voice/transcriptions").status_code == 401
    assert client.delete(f"/api/v1/voice/sessions/{grant['id']}").status_code == 204
    assert post(grant, wav()).status_code == 401
