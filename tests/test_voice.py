import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from accounts.models import UserProfile
from coordinator.models import Task, VoiceConversation, VoiceToolResult
from coordinator.voice import provider_request
from coordinator.voice_policy import tool_configs

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    user = get_user_model().objects.create_user("pm-owner", password="test-password")
    UserProfile.objects.update_or_create(user=user, defaults={"role": "owner"})
    return user


@pytest.fixture
def voice(owner, settings, monkeypatch):
    settings.ELEVENLABS_API_KEY = "provider-secret-must-stay-server-only"
    settings.ELEVENLABS_AGENT_ID = "agent_fixture"
    monkeypatch.setattr(
        "coordinator.voice.provider_request",
        lambda *args, **kwargs: {
            "signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?fixture=true"
        },
    )
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
    assert settings.ELEVENLABS_API_KEY not in str(grant)
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
    assert client.get("/api/v1/voice/status").json()["provider"] == "elevenlabs"


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

    settings.ELEVENLABS_API_KEY = "test-private-provider-key"
    monkeypatch.setattr(
        "coordinator.voice.httpx.request",
        lambda *args, **kwargs: httpx.Response(401, json={"detail": "test-private-provider-key"}),
    )
    with pytest.raises(Exception, match="HTTP 401") as error:
        provider_request("GET", "agents")
    assert settings.ELEVENLABS_API_KEY not in str(error.value)


def test_tool_definitions_keep_auth_outside_model():
    configs = tool_configs("https://atm.example")
    assert len(configs) == 7
    for config in configs:
        assert config["api_schema"]["request_headers"]["X-ATM-Voice-Token"] == {
            "variable_name": "secret__atm_voice_token"
        }
        properties = config["api_schema"]["request_body_schema"]["properties"]
        assert "user_id" not in properties
        assert properties["conversation_id"] == {
            "type": "string",
            "dynamic_variable": "system__conversation_id",
        }
