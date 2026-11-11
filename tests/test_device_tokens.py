"""Per-device API tokens for mobile clients (#48)."""

import logging

import pytest
from django.core.cache import cache
from django.test import Client

from accounts.models import AccountEvent, Role, UserToken
from accounts.services import hash_token, set_role

PASSWORD = "a-Mobile-passphrase-99"


@pytest.fixture(autouse=True)
def clear_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def member(django_user_model):
    user = django_user_model.objects.create_user("phone-user", password=PASSWORD)
    set_role(user, Role.MEMBER, actor="test")
    return user


def issue(username="phone-user", password=PASSWORD, name="Ana's iPhone"):
    # Mobile clients send no cookies and no CSRF token.
    return Client(enforce_csrf_checks=True).post(
        "/api/v1/auth/tokens",
        {"username": username, "password": password, "name": name},
        content_type="application/json",
    )


def device(raw):
    return Client(HTTP_AUTHORIZATION=f"Token {raw}")


@pytest.mark.django_db
def test_issue_shows_the_token_once_and_stores_only_its_hash(member, caplog):
    with caplog.at_level(logging.DEBUG):
        response = issue()
    assert response.status_code == 201, response.content
    body = response.json()
    raw = body["token"]
    assert body["user"]["role"] == "member" and body["name"] == "Ana's iPhone"
    token = UserToken.objects.get()
    assert token.key_hash == hash_token(raw) and token.key_prefix == raw[:8]
    assert raw not in caplog.text and PASSWORD not in caplog.text
    event = AccountEvent.objects.get(event_type="token.created")
    assert raw not in event.message
    listed = device(raw).get("/api/v1/auth/tokens")
    assert raw not in listed.content.decode()
    assert listed.json()[0]["current"] is True


@pytest.mark.django_db
def test_token_authenticates_as_the_user_with_their_role(member, project):
    raw = issue().json()["token"]
    client = device(raw)
    assert client.get("/api/v1/auth/me").json()["username"] == "phone-user"
    created = client.post(
        "/api/v1/tasks",
        {"title": "From phone", "project": "coordinator"},
        content_type="application/json",
    )
    assert created.status_code == 201  # no CSRF for header-authenticated requests
    assert UserToken.objects.get().last_used_at is not None


@pytest.mark.django_db
def test_viewer_tokens_stay_read_only(member, project):
    set_role(member, Role.VIEWER, actor="test")
    client = device(issue().json()["token"])
    assert client.get("/api/v1/tasks").status_code == 200
    denied = client.post(
        "/api/v1/tasks", {"title": "x", "project": "coordinator"}, content_type="application/json"
    )
    assert denied.status_code == 403


@pytest.mark.django_db
def test_bad_credentials_and_inactive_users_get_no_token(member):
    assert issue(password="wrong").status_code == 400
    assert issue(username="ghost").json() == {"detail": "Invalid username or password."}
    member.is_active = False
    member.save()
    assert issue().status_code == 400
    assert not UserToken.objects.exists()


@pytest.mark.django_db
def test_revoking_the_current_token_logs_the_device_out(member):
    raw = issue().json()["token"]
    other = issue(name="tablet").json()["token"]
    client = device(raw)
    assert client.post("/api/v1/auth/tokens/current/revoke").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    assert device(other).get("/api/v1/auth/me").status_code == 200
    assert AccountEvent.objects.filter(event_type="token.revoked").count() == 1


@pytest.mark.django_db
def test_users_revoke_only_their_own_tokens_and_owners_revoke_any(member, django_user_model):
    raw = issue().json()["token"]
    token_id = UserToken.objects.get().pk
    stranger = django_user_model.objects.create_user("stranger", password=PASSWORD)
    set_role(stranger, Role.MEMBER, actor="test")
    stranger_raw = issue("stranger", name="laptop").json()["token"]
    assert device(stranger_raw).post(f"/api/v1/auth/tokens/{token_id}/revoke").status_code == 404
    assert device(stranger_raw).get("/api/v1/auth/tokens?all=true").json()[0]["username"] == (
        "stranger"
    )

    owner = django_user_model.objects.create_user("boss", is_superuser=True)
    boss = Client()
    boss.force_login(owner)
    assert len(boss.get("/api/v1/auth/tokens?all=true").json()) == 2
    assert boss.post(f"/api/v1/auth/tokens/{token_id}/revoke").status_code == 200
    assert device(raw).get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_deactivation_and_owner_password_reset_revoke_tokens(member, django_user_model):
    owner = django_user_model.objects.create_user("boss", is_superuser=True)
    boss = Client()
    boss.force_login(owner)
    raw = issue().json()["token"]
    boss.patch(f"/api/v1/users/{member.pk}", {"is_active": False}, content_type="application/json")
    assert device(raw).get("/api/v1/auth/me").status_code == 401
    boss.patch(f"/api/v1/users/{member.pk}", {"is_active": True}, content_type="application/json")
    assert device(raw).get("/api/v1/auth/me").status_code == 401  # stays revoked

    fresh = issue().json()["token"]
    boss.post(
        f"/api/v1/users/{member.pk}/password",
        {"password": "a-Reset-passphrase-11"},
        content_type="application/json",
    )
    assert device(fresh).get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_token_and_agent_schemes_stay_distinct(member, api_client):
    raw = issue().json()["token"]
    # A device token is not an agent key, and vice versa.
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get("/api/v1/tasks").status_code == 401
    assert device("x" * 43).get("/api/v1/tasks").status_code == 401
    assert Client(HTTP_AUTHORIZATION="Token").get("/api/v1/tasks").status_code == 401
    assert api_client.get("/api/v1/auth/tokens").status_code == 403  # agents have no tokens


@pytest.mark.django_db
def test_token_issue_is_throttled(member):
    from accounts.api import LoginThrottle

    original = LoginThrottle.THROTTLE_RATES
    LoginThrottle.THROTTLE_RATES = {**original, "login": "2/min"}
    try:
        assert [issue(password="wrong").status_code for _ in range(3)] == [400, 400, 429]
    finally:
        LoginThrottle.THROTTLE_RATES = original


@pytest.mark.django_db
def test_schema_documents_the_device_token_scheme(api_client):
    schema = api_client.get("/api/v1/schema?format=json").json()
    assert schema["components"]["securitySchemes"]["deviceToken"]["in"] == "header"
    assert "/api/v1/auth/tokens" in schema["paths"]
