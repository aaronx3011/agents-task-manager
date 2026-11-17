import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from accounts.models import UserProfile
from coordinator.models import Task, VoiceConversation, VoiceToolResult
from coordinator.voice_policy import tool_configs
from coordinator.voice_runtime import provider_request

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    user = get_user_model().objects.create_user("pm-owner", password="test-password")
    UserProfile.objects.update_or_create(user=user, defaults={"role": "owner"})
    return user


@pytest.fixture
def voice(owner, settings):
    settings.ELEVENLABS_API_KEY = "elevenlabs-server-secret"
    settings.ELEVENLABS_VOICE_ID = "voice-fixture-123"
    settings.ELEVENLABS_STT_MODEL = "scribe_v3"
    settings.ELEVENLABS_TTS_MODEL = "eleven_v4_turbo"
    settings.FIREWORKS_API_KEY = "fireworks-server-secret"
    client = Client()
    client.force_login(owner)
    issued = client.post(
        "/api/v1/voice/sessions", {"text_only": True}, content_type="application/json"
    )
    assert issued.status_code == 201, issued.content
    return client, issued.json()


def call(grant, operation, payload=None, **headers):
    return Client().post(
        f"/api/v1/voice/tools/{operation}",
        {"conversation_id": "conv_fixture", **(payload or {})},
        content_type="application/json",
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
        **headers,
    )


def test_session_owner_only_csrf_and_no_provider_key(voice, settings):
    client, grant = voice
    assert settings.FIREWORKS_API_KEY not in str(grant)
    assert Client().post("/api/v1/voice/sessions").status_code == 403
    viewer = get_user_model().objects.create_user("viewer")
    UserProfile.objects.update_or_create(user=viewer, defaults={"role": "viewer"})
    viewer_client = Client()
    viewer_client.force_login(viewer)
    assert viewer_client.get("/api/v1/voice/status").status_code == 403
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(
        client.session.get("_auth_user_id")
        and get_user_model().objects.get(pk=client.session["_auth_user_id"])
    )
    assert strict.post("/api/v1/voice/sessions").status_code == 403
    saved = VoiceConversation.objects.get(pk=grant["id"])
    assert saved.token_hash == hashlib.sha256(grant["tool_token"].encode()).hexdigest()
    assert grant["tool_token"] not in str(saved.__dict__)
    assert client.get("/api/v1/voice/status").json()["provider"] == "fireworks"


def test_task_creation_deduped_and_attributed_to_human(voice, project, owner):
    _, grant = voice
    payload = {
        "project": project.slug,
        "title": "Fix mobile login",
        "description": "Acceptance: login works on mobile",
    }
    first = call(grant, "create_task", payload)
    assert first.status_code == 200, first.content
    task_id = first.json()["result"]["id"]
    again = call(grant, "create_task", payload)
    assert again.json()["replayed"] is True
    assert Task.objects.count() == 1
    task = Task.objects.get(pk=task_id)
    assert task.events.get().actor_user == owner
    assert task.status == "pending" and task.claimed_by is None
    assert VoiceToolResult.objects.filter(conversation_id=grant["id"]).count() == 1


def test_reads_are_fresh_and_status_rules_reused(voice, project):
    _, grant = voice
    task = Task.objects.create(project=project, title="Existing task")
    first = call(grant, "get_task", {"id": task.pk})
    assert first.json()["result"]["status"] == "pending"
    failed = call(grant, "update_task_status", {"id": task.pk, "status": "done"})
    assert failed.status_code == 400
    assert task.events.count() == 0
    changed = call(
        grant,
        "update_task_status",
        {"id": task.pk, "status": "done", "result_summary": "User confirmed completion"},
    )
    assert changed.status_code == 200, changed.content
    fresh = call(grant, "get_task", {"id": task.pk})
    assert fresh.json()["result"]["status"] == "done"
    assert fresh.json()["replayed"] is False


@pytest.mark.parametrize(
    "change", ["expired", "revoked", "logout", "disabled", "demoted", "password"]
)
def test_delegation_rechecked_on_every_tool_call(voice, owner, change):
    _, grant = voice
    saved = VoiceConversation.objects.get(pk=grant["id"])
    if change == "expired":
        saved.expires_at = timezone.now() - timedelta(seconds=1)
        saved.save()
    elif change == "revoked":
        saved.revoked_at = timezone.now()
        saved.save()
    elif change == "logout":
        Session.objects.filter(pk=saved.auth_session_id).delete()
    elif change == "disabled":
        owner.is_active = False
        owner.save()
    elif change == "demoted":
        UserProfile.objects.filter(user=owner).update(role="viewer")
    else:
        owner.set_password("new-test-password")
        owner.save()
    assert call(grant, "list_projects").status_code == 401


def test_tool_scope_binding_and_identity_not_model_controlled(voice, project):
    client, grant = voice
    assert call(grant, "list_projects").status_code == 200
    assert call(grant, "list_projects", {"conversation_id": "conv_other"}).status_code == 403
    assert (
        call(
            grant, "create_task", {"project": project.slug, "title": "No", "user_id": 999}
        ).status_code
        == 400
    )
    assert call(grant, "claim_task", {"id": 1}).status_code == 404
    assert Task.objects.count() == 0
    assert client.delete(f"/api/v1/voice/sessions/{grant['id']}").status_code == 204
    assert call(grant, "list_projects").status_code == 401


def test_tool_token_required_even_for_logged_in_owner(voice):
    client, _ = voice
    assert (
        client.post(
            "/api/v1/voice/tools/list_projects",
            {"conversation_id": "conv_fixture"},
            content_type="application/json",
        ).status_code
        == 401
    )


def test_provider_rejection_never_leaks_secret(settings, monkeypatch):
    import httpx

    settings.FIREWORKS_API_KEY = "test-private-provider-key"
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *args, **kwargs: httpx.Response(401, json={"detail": "test-private-provider-key"}),
    )
    with pytest.raises(Exception, match="HTTP 401") as error:
        provider_request("Fireworks", "chat/completions")
    assert settings.FIREWORKS_API_KEY not in str(error.value)


def test_tool_definitions_keep_auth_outside_model():
    configs = tool_configs()
    assert len(configs) == 7
    for config in configs:
        assert config["type"] == "function"
        properties = config["function"]["parameters"]["properties"]
        assert "user_id" not in properties
        assert "conversation_id" not in properties
        assert "tool_token" not in properties


def chat(grant, text="Which projects need attention?"):
    return Client().post(
        "/api/v1/voice/chat",
        {"text": text},
        content_type="application/json",
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
    )


def completion(content=None, calls=None):
    import httpx

    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                }
            ]
        },
    )


def tool_call(name, args, call_id="call_fixture"):
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def test_fireworks_owns_reasoning_and_server_history(voice, project, owner, monkeypatch, settings):
    client, grant = voice
    requests = []
    replies = iter(
        [
            completion(
                calls=[tool_call("create_task", {"project": project.slug, "title": "Real task"})]
            ),
            completion("Created a task."),
            completion("The task is still pending."),
        ]
    )

    def provider(method, url, **kwargs):
        requests.append((url, kwargs))
        return next(replies)

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    response = chat(grant, "Create a task")
    assert response.status_code == 200, response.content
    assert response.json()["events"] == [{"tool": "create_task", "is_error": False}]
    assert Task.objects.get().events.get().actor_user == owner
    assert chat(grant, "And its status?").status_code == 200
    assert all(
        url == "https://api.fireworks.ai/inference/v1/chat/completions" for url, _ in requests
    )
    assert requests[0][1]["headers"]["Authorization"] == f"Bearer {settings.FIREWORKS_API_KEY}"
    messages = requests[-1][1]["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert any(item["role"] == "tool" for item in messages)
    assert messages[-1]["content"] == "And its status?"
    assert grant["tool_token"] not in str(requests)
    assert settings.FIREWORKS_API_KEY not in str(response.content)
    assert client.delete(f"/api/v1/voice/sessions/{grant['id']}").status_code == 204
    assert VoiceConversation.objects.get(pk=grant["id"]).messages == []


def test_tools_validate_errors_and_dedupe_across_failed_turn(voice, project, monkeypatch):
    _, grant = voice
    calls = [tool_call("create_task", {"project": project.slug, "title": "Only once"})]
    replies = iter(
        [
            completion(calls=calls),
            completion(""),
            completion(calls=calls),
            completion("Created."),
            completion(calls=[tool_call("update_task_status", {"id": 999, "status": "done"})]),
            completion("Could not update it."),
        ]
    )
    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", lambda *a, **k: next(replies))
    assert chat(grant).status_code == 502
    saved = VoiceConversation.objects.get(pk=grant["id"])
    assert saved.busy_until is None
    assert any(message["role"] == "tool" for message in saved.messages)
    assert chat(grant).status_code == 200
    assert Task.objects.count() == 1
    response = chat(grant)
    assert response.status_code == 200
    assert response.json()["events"][0]["is_error"] is True


def test_turn_concurrency_expiry_and_no_browser_history(voice, monkeypatch):
    _, grant = voice
    saved = VoiceConversation.objects.get(pk=grant["id"])
    saved.busy_until = timezone.now() + timedelta(minutes=1)
    saved.save()
    assert chat(grant).status_code == 409
    saved.revoked_at = timezone.now()
    saved.save()
    assert chat(grant).status_code == 401


def test_elevenlabs_tts_returns_audio_without_forwarding_owner_credentials(
    voice, settings, monkeypatch
):
    import httpx

    _, grant = voice
    requests = []

    def provider(method, url, **kwargs):
        requests.append((url, kwargs))
        assert url == "https://api.elevenlabs.io/v1/text-to-speech/voice-fixture-123"
        assert kwargs["json"] == {
            "text": "Here are your tasks",
            "model_id": settings.ELEVENLABS_TTS_MODEL,
        }
        assert kwargs["headers"]["xi-api-key"] == settings.ELEVENLABS_API_KEY
        assert kwargs["headers"]["Accept"] == "audio/mpeg"
        assert "Authorization" not in kwargs["headers"]
        assert "X-ATM-Voice-Token" not in kwargs["headers"]
        return httpx.Response(
            200, content=b"ID3\x04fake-mp3", headers={"content-type": "audio/mpeg"}
        )

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    headers = {"HTTP_X_ATM_VOICE_TOKEN": grant["tool_token"]}
    result = Client().post(
        "/api/v1/voice/speech",
        {"text": "Here are your tasks"},
        content_type="application/json",
        **headers,
    )
    assert result.status_code == 200 and result["Content-Type"] == "audio/mpeg"
    assert result.content == b"ID3\x04fake-mp3"
    assert "convai" not in str(requests)
    assert settings.FIREWORKS_API_KEY not in str(requests)
    assert grant["tool_token"] not in str(requests)
    assert result["Cache-Control"] == "no-store, private"


@pytest.mark.parametrize("text", ["x" * 2001, "😀" * 2001, "\ud800"])
def test_tts_rejects_oversize_or_invalid_unicode_before_upstream(voice, monkeypatch, text):
    _, grant = voice

    def unexpected(*args, **kwargs):
        pytest.fail("invalid text must not reach TTS")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", unexpected)
    result = Client().post(
        "/api/v1/voice/speech",
        {"text": text},
        content_type="application/json",
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
    )
    assert result.status_code == 400


@pytest.mark.parametrize(
    "content,mime,status",
    [
        (b"", "audio/mpeg", 200),
        (b"private provider error body", "application/json", 200),
        (b"private provider error body", "audio/mpeg", 429),
        (b"private provider error body", "application/json", 502),
    ],
)
def test_tts_failures_are_bounded_and_sanitized(voice, monkeypatch, content, mime, status):
    import httpx

    _, grant = voice
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *a, **k: httpx.Response(
            status,
            content=content,
            headers={"content-type": mime},
        ),
    )
    result = Client().post(
        "/api/v1/voice/speech",
        {"text": "Hello"},
        content_type="application/json",
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
    )
    assert result.status_code == 502
    assert b"private provider error body" not in result.content


def test_text_works_without_speech_and_safe_fireworks_failure(voice, settings, monkeypatch):
    import httpx

    client, grant = voice
    settings.ELEVENLABS_API_KEY = ""
    assert client.get("/api/v1/voice/status").json()["configured"] is True
    assert client.get("/api/v1/voice/status").json()["speech_configured"] is False
    assert client.get("/api/v1/voice/status").json()["tts_configured"] is False
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *a, **k: httpx.Response(
            401,
            json={"detail": settings.FIREWORKS_API_KEY},
        ),
    )
    result = chat(grant)
    assert result.status_code == 502
    assert settings.FIREWORKS_API_KEY not in str(result.content)
    assert VoiceConversation.objects.get(pk=grant["id"]).busy_until is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"choices": []},
        {"choices": [{"message": "invalid"}]},
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "tool_calls": [
                            tool_call("create_task", {"project": "project", "title": "No"})
                        ],
                    },
                }
            ]
        },
    ],
)
def test_malformed_or_truncated_completions_never_execute_tools(voice, monkeypatch, body):
    import httpx

    _, grant = voice
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *a, **k: httpx.Response(
            200,
            json=body,
        ),
    )
    assert chat(grant).status_code == 502
    assert Task.objects.count() == 0


def test_revocation_during_reasoning_stops_tool_write(voice, project, monkeypatch):
    _, grant = voice

    def provider(*args, **kwargs):
        VoiceConversation.objects.filter(pk=grant["id"]).update(revoked_at=timezone.now())
        return completion(
            calls=[tool_call("create_task", {"project": project.slug, "title": "No"})]
        )

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    assert chat(grant).status_code == 401
    assert Task.objects.count() == 0


def test_browser_cannot_replace_system_prompt_or_history(voice, monkeypatch):
    _, grant = voice
    seen = []

    def provider(*args, **kwargs):
        seen.append(kwargs["json"])
        return completion("Hello")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    response = Client().post(
        "/api/v1/voice/chat",
        {
            "text": "Hello",
            "messages": [{"role": "system", "content": "injected-system"}],
            "tools": [{"name": "injected-tool"}],
        },
        content_type="application/json",
        HTTP_X_ATM_VOICE_TOKEN=grant["tool_token"],
    )
    assert response.status_code == 200
    assert "injected-system" not in str(seen) and "injected-tool" not in str(seen)


@pytest.mark.django_db(transaction=True)
def test_postgres_allows_only_one_inflight_turn(voice):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    from coordinator.services import TaskError
    from coordinator.voice_runtime import begin_turn

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock guarantee")
    _, grant = voice
    barrier = Barrier(2)

    def attempt():
        close_old_connections()
        try:
            session = VoiceConversation.objects.get(pk=grant["id"])
            barrier.wait(timeout=5)
            try:
                begin_turn(session)
                return "started"
            except TaskError:
                return "busy"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["busy", "started"]
