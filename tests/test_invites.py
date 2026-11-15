"""Owner-created invite links, shared manually (#46)."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from accounts.models import AccountEvent, Invite, RoleChange
from accounts.services import accept_invite, create_invite, hash_token, role_of

User = get_user_model()
PASSWORD = "a-Long-invitee-passphrase-7"


@pytest.fixture(autouse=True)
def clear_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(django_user_model):
    user = django_user_model.objects.create_user("owner", is_superuser=True, is_staff=True)
    client = Client()
    client.force_login(user)
    return user, client


def browser():
    client = Client(enforce_csrf_checks=True)
    return client, client.get("/api/v1/auth/csrf").json()["csrfToken"]


def public(client, token, path, body):
    return client.post(
        f"/api/v1/invites/{path}", body, content_type="application/json", HTTP_X_CSRFTOKEN=token
    )


def new_invite(client, **body):
    response = client.post("/api/v1/invites", body, content_type="application/json")
    assert response.status_code == 201, response.content
    return response.json()


@pytest.mark.django_db
def test_owner_creates_a_link_shown_once_and_stored_hashed(owner):
    _, client = owner
    created = new_invite(client, role="viewer", note="for QA")
    raw = created["token"]
    assert created["url"].endswith(f"/invite#{raw}")  # fragment: never sent to the server
    assert created["status"] == "pending" and created["role"] == "viewer"
    invite = Invite.objects.get(pk=created["id"])
    assert invite.token_hash == hash_token(raw) and raw not in invite.token_hash
    assert invite.token_prefix == raw[:8]
    listed = client.get("/api/v1/invites")
    assert listed.status_code == 200
    assert raw not in listed.content.decode()
    assert listed.json()[0]["token_prefix"] == raw[:8]
    event = AccountEvent.objects.get(event_type="invite.created")
    assert raw not in event.message and event.actor == "owner"


@pytest.mark.django_db
def test_accepting_creates_the_user_with_the_role_and_signs_in(owner):
    _, client = owner
    raw = new_invite(client, role="member")["token"]
    visitor, token = browser()
    preview = public(visitor, token, "check", {"token": raw})
    assert preview.status_code == 200 and preview.json()["role"] == "member"

    accepted = public(
        visitor, token, "accept", {"token": raw, "username": "newbie", "password": PASSWORD}
    )
    assert accepted.status_code == 201, accepted.content
    assert accepted.json()["role"] == "member"
    assert visitor.get("/api/v1/auth/me").json()["username"] == "newbie"
    user = User.objects.get(username="newbie")
    assert role_of(user) == "member" and user.check_password(PASSWORD) and not user.is_staff
    invite = Invite.objects.get()
    assert invite.used_by == user and invite.used_at
    assert AccountEvent.objects.filter(event_type="invite.accepted", subject_user=user).exists()
    assert RoleChange.objects.filter(user=user, new_role="member", actor=f"invite:{invite.pk}")


@pytest.mark.django_db
def test_used_revoked_expired_and_unknown_links_are_rejected_alike(owner):
    owner_user, client = owner
    used = new_invite(client)["token"]
    accept_invite(used, "first", PASSWORD)
    revoked = new_invite(client)
    assert client.post(f"/api/v1/invites/{revoked['id']}/revoke").json()["status"] == "revoked"
    assert client.post(f"/api/v1/invites/{revoked['id']}/revoke").status_code == 409
    expired, raw_expired = create_invite(owner_user, "member", hours=1)
    Invite.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

    visitor, token = browser()
    for raw in [used, revoked["token"], raw_expired, "not-a-real-token"]:
        for path, body in [
            ("check", {"token": raw}),
            ("accept", {"token": raw, "username": f"u{len(raw)}", "password": PASSWORD}),
        ]:
            response = public(visitor, token, path, body)
            assert response.status_code == 410, (raw, path)
            assert response.json() == {"detail": "This invite link is invalid or has expired."}
    assert set(User.objects.values_list("username", flat=True)) == {"owner", "first"}


@pytest.mark.django_db
def test_failed_accept_leaves_the_invite_usable(owner):
    _, client = owner
    raw = new_invite(client)["token"]
    visitor, token = browser()
    weak = public(visitor, token, "accept", {"token": raw, "username": "x", "password": "123"})
    assert weak.status_code == 400
    taken = public(
        visitor, token, "accept", {"token": raw, "username": "OWNER", "password": PASSWORD}
    )
    assert taken.status_code == 409
    assert Invite.objects.get().used_at is None
    ok = public(visitor, token, "accept", {"token": raw, "username": "later", "password": PASSWORD})
    assert ok.status_code == 201


@pytest.mark.django_db
def test_invite_management_is_owner_only_and_public_routes_need_csrf(owner, django_user_model):
    _, client = owner
    raw = new_invite(client)["token"]
    member = Client()
    member.force_login(django_user_model.objects.create_user("member", is_staff=True))
    assert member.get("/api/v1/invites").status_code == 403
    assert member.post("/api/v1/invites", {}, content_type="application/json").status_code == 403
    assert Client().get("/api/v1/invites").status_code in {401, 403}
    no_csrf = Client(enforce_csrf_checks=True).post(
        "/api/v1/invites/check", {"token": raw}, content_type="application/json"
    )
    assert no_csrf.status_code == 403


@pytest.mark.django_db
def test_raw_tokens_never_reach_logs(owner, caplog):
    _, client = owner
    with caplog.at_level(logging.DEBUG):
        raw = new_invite(client)["token"]
        visitor, token = browser()
        public(visitor, token, "accept", {"token": raw, "username": "quiet", "password": PASSWORD})
    assert raw not in caplog.text and PASSWORD not in caplog.text


@pytest.mark.django_db
def test_public_invite_routes_are_throttled(owner, settings):
    from rest_framework.settings import api_settings

    from accounts.api import InviteThrottle

    original = InviteThrottle.THROTTLE_RATES
    InviteThrottle.THROTTLE_RATES = {**api_settings.DEFAULT_THROTTLE_RATES, "invite": "2/min"}
    try:
        visitor, token = browser()
        codes = [public(visitor, token, "check", {"token": "nope"}).status_code for _ in range(3)]
        assert codes == [410, 410, 429]
    finally:
        InviteThrottle.THROTTLE_RATES = original


@pytest.mark.django_db(transaction=True)
def test_concurrent_accepts_consume_the_invite_once(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock race requires PostgreSQL")
    owner = django_user_model.objects.create_user("race-owner", is_superuser=True)
    _, raw = create_invite(owner, "member")
    barrier = Barrier(2)

    def attempt(name):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                accept_invite(raw, name, PASSWORD)
                return "created"
            except Exception as exc:  # AccountError for the loser
                return getattr(exc, "status", repr(exc))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(map(str, pool.map(attempt, ["racer-a", "racer-b"])))
    assert results == ["410", "created"]
    assert User.objects.filter(username__startswith="racer-").count() == 1
    assert Invite.objects.get().used_by.username.startswith("racer-")
