"""PM voice conversations started from the mobile app with a device token (task #352)."""

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import Role, UserProfile, UserToken
from accounts.services import issue_token, set_password, set_role
from coordinator.models import VoiceConversation
from tests.test_voice import call, owner  # noqa: F401  (shared fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture
def configured(settings):
    settings.FIREWORKS_API_KEY = "fireworks-server-secret"
    settings.ELEVENLABS_API_KEY = "elevenlabs-server-secret"
    settings.ELEVENLABS_VOICE_ID = "voice-fixture-123"


def device(raw):
    # Like the app: no cookies, no CSRF token, no Origin header.
    return Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Token {raw}")


@pytest.fixture
def phone(owner, configured):  # noqa: F811
    token, raw = issue_token(owner, "Owner's phone")
    issued = device(raw).post(
        "/api/v1/voice/sessions", {"text_only": True}, content_type="application/json"
    )
    assert issued.status_code == 201, issued.content
    return token, raw, issued.json()


def test_device_token_starts_a_conversation_bound_to_that_device(phone):
    token, raw, grant = phone
    saved = VoiceConversation.objects.get(pk=grant["id"])
    assert saved.auth_token == token and saved.auth_session is None
    assert raw not in str(saved.__dict__)
    assert device(raw).get("/api/v1/voice/status").json()["configured"] is True
    assert call(grant, "list_projects").status_code == 200
    assert device(raw).delete(f"/api/v1/voice/sessions/{grant['id']}").status_code == 204
    assert call(grant, "list_projects").status_code == 401


@pytest.mark.parametrize("change", ["revoked", "password", "demoted", "disabled"])
def test_device_conversation_ends_with_its_credential(phone, owner, change):  # noqa: F811
    token, _, grant = phone
    if change == "revoked":
        UserToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
    elif change == "password":
        set_password(owner, "an-Entirely-new-passphrase-42")
    elif change == "demoted":
        UserProfile.objects.filter(user=owner).update(role=Role.MEMBER)
    else:
        owner.is_active = False
        owner.save()
    assert call(grant, "list_projects").status_code == 401


def test_device_tokens_of_non_owners_cannot_start_conversations(configured, django_user_model):
    member = django_user_model.objects.create_user("phone-member")
    set_role(member, Role.MEMBER, actor="test")
    _, raw = issue_token(member, "Member phone")
    assert device(raw).get("/api/v1/voice/status").status_code == 403
    assert device(raw).post("/api/v1/voice/sessions").status_code == 403


def test_agent_keys_cannot_start_conversations(configured, agent_clients):
    client = agent_clients[0]
    assert client.post("/api/v1/voice/sessions").status_code in {401, 403}


def test_conversation_holds_exactly_one_credential(phone, owner):  # noqa: F811
    _, _, grant = phone
    with pytest.raises(IntegrityError), transaction.atomic():
        VoiceConversation.objects.filter(pk=grant["id"]).update(auth_token=None)
