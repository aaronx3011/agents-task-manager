"""Session login/logout/me API (#45)."""

import pytest
from django.core.cache import cache
from django.test import Client

from accounts.models import Role
from accounts.services import set_role

PASSWORD = "a-Long-test-passphrase-42"


@pytest.fixture(autouse=True)
def clear_throttle():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def member(django_user_model):
    user = django_user_model.objects.create_user("member1", "m@example.test", PASSWORD)
    set_role(user, Role.MEMBER, actor="test")
    return user


def browser():
    """A CSRF-enforcing client primed with the csrftoken cookie, like the SPA."""
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    return client, token


def login(client, token, username="member1", password=PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        {"username": username, "password": password},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


@pytest.mark.django_db
def test_csrf_endpoint_sets_cookie_and_is_not_cached():
    client = Client(enforce_csrf_checks=True)
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    assert response.json()["csrfToken"]
    assert "csrftoken" in response.cookies
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.django_db
def test_login_starts_a_session_and_me_reports_the_role(member):
    client, token = browser()
    response = login(client, token)
    assert response.status_code == 200
    assert response.json() == {
        "id": member.pk,
        "username": "member1",
        "email": "m@example.test",
        "role": "member",
        "is_superuser": False,
    }
    assert PASSWORD not in response.content.decode()
    assert client.get("/api/v1/auth/me").json()["role"] == "member"
    assert client.get("/api/v1/tasks").status_code == 200


@pytest.mark.django_db
def test_login_rotates_the_session_key(member):
    client, token = browser()
    client.get("/api/v1/auth/me")
    before = client.cookies.get("sessionid")
    login(client, token)
    assert client.cookies["sessionid"].value != (before.value if before else None)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("username", "password"), [("member1", "wrong"), ("nobody", PASSWORD), ("", "")]
)
def test_bad_credentials_get_one_generic_answer(member, username, password):
    client, token = browser()
    response = login(client, token, username, password)
    assert response.status_code == 400
    if username:
        assert response.json() == {"detail": "Invalid username or password."}
    assert client.get("/api/v1/auth/me").status_code in {401, 403}


@pytest.mark.django_db
def test_inactive_user_cannot_log_in(member):
    member.is_active = False
    member.save()
    client, token = browser()
    response = login(client, token)
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid username or password."}


@pytest.mark.django_db
def test_login_and_logout_enforce_csrf(member):
    client, token = browser()
    no_token = client.post(
        "/api/v1/auth/login",
        {"username": "member1", "password": PASSWORD},
        content_type="application/json",
    )
    assert no_token.status_code == 403
    assert "CSRF" in no_token.json()["detail"]
    assert login(client, token).status_code == 200
    rotated = client.cookies["csrftoken"].value
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout", HTTP_X_CSRFTOKEN=rotated).status_code == 204


@pytest.mark.django_db
def test_logout_invalidates_the_session(member):
    client, token = browser()
    login(client, token)
    stolen = client.cookies["sessionid"].value
    token = client.cookies["csrftoken"].value
    assert client.post("/api/v1/auth/logout", HTTP_X_CSRFTOKEN=token).status_code == 204
    assert client.get("/api/v1/auth/me").status_code in {401, 403}
    replay = Client()
    replay.cookies["sessionid"] = stolen
    assert replay.get("/api/v1/tasks").status_code in {401, 403}


@pytest.mark.django_db
def test_login_attempts_are_throttled_per_address_and_username(member, monkeypatch):
    from accounts.api import LoginThrottle

    # Patch the class attribute only; monkeypatch restores it for later tests.
    monkeypatch.setattr(
        LoginThrottle, "THROTTLE_RATES", {**LoginThrottle.THROTTLE_RATES, "login": "3/min"}
    )
    client, token = browser()
    statuses = [login(client, token, password="wrong").status_code for _ in range(4)]
    assert statuses == [400, 400, 400, 429]
    assert login(client, token).status_code == 429  # correct password still waits
    other, other_token = browser()
    assert login(other, other_token, username="someone-else").status_code == 400


@pytest.mark.django_db
def test_me_requires_a_human_session(api_client):
    assert Client().get("/api/v1/auth/me").status_code in {401, 403}
    assert api_client.get("/api/v1/auth/me").status_code == 403


@pytest.mark.django_db
def test_auth_endpoints_are_in_the_schema(api_client):
    paths = api_client.get("/api/v1/schema?format=json").json()["paths"]
    for path in ["csrf", "login", "logout", "me"]:
        assert f"/api/v1/auth/{path}" in paths
