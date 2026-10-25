from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client

from coordinator.models import Agent

from .test_api import create, post

pytestmark = pytest.mark.django_db


@pytest.fixture
def registration_staff(django_user_model):
    client = Client(enforce_csrf_checks=True)
    user = django_user_model.objects.create_user("registrar", is_staff=True)
    client.force_login(user)
    client.get("/dashboard/")
    return client


def register(client, name, **extra):
    return client.post(
        "/api/agents",
        {"name": name},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        **extra,
    )


def test_key_is_shown_once_stored_hashed_and_identifies_the_new_agent(
    registration_staff,
    api_client,
    project,
):
    response = register(registration_staff, "  workstation-reviewer  ")
    assert response.status_code == 201
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Pragma"] == "no-cache"
    data = response.json()
    assert data["name"] == "workstation-reviewer"
    assert data["is_active"] and data["last_seen"] is None
    key = data["api_key"]
    agent = Agent.objects.get(name=data["name"])
    assert key != agent.api_key_hash and key not in agent.api_key_hash
    assert Agent.authenticate(key).pk == agent.pk
    assert Agent.authenticate("x" * 43) is None
    new_client = Client(HTTP_AUTHORIZATION=f"Bearer {key}")
    task = create(api_client)
    claimed = post(new_client, f"tasks/{task['id']}/claim").json()
    assert claimed["claimed_by"] == "workstation-reviewer"
    for path in ["/api/agents", "/dashboard/workspace/", "/api/overview"]:
        listed = registration_staff.get(path)
        body = listed.content.decode()
        assert key not in body and "api_key_hash" not in body and "key_prefix" not in body
    assert "no-store" in registration_staff.get("/api/agents").headers["Cache-Control"]


def test_duplicate_name_never_rotates_or_reactivates_an_existing_agent(registration_staff):
    created = register(registration_staff, "existing").json()
    agent = Agent.objects.get(name="existing")
    original_hash = agent.api_key_hash
    agent.is_active = False
    agent.save()
    duplicate = register(registration_staff, " existing ")
    assert duplicate.status_code == 409
    assert "api_key" not in duplicate.json()
    agent.refresh_from_db()
    assert agent.api_key_hash == original_hash and not agent.is_active
    assert Agent.objects.filter(name="existing").count() == 1
    assert Agent.authenticate(created["api_key"]) is None


@pytest.mark.parametrize("name", ["", "   ", None, "x" * 101])
def test_invalid_name_creates_no_identity(registration_staff, name):
    count = Agent.objects.count()
    response = register(registration_staff, name)
    assert response.status_code == 400
    assert "api_key" not in response.json()
    assert Agent.objects.count() == count


def test_agent_credentials_and_nonstaff_sessions_cannot_register_or_list(
    api_client,
    django_user_model,
):
    ordinary = Client()
    ordinary.force_login(django_user_model.objects.create_user("ordinary-registrar"))
    for client in [Client(), ordinary, api_client]:
        assert client.get("/api/agents").status_code in {401, 403}
        assert client.post("/api/agents", {"name": "denied"}).status_code in {401, 403}
    assert not Agent.objects.filter(name="denied").exists()


def test_registration_enforces_csrf_and_invalid_bearer_never_falls_back_to_staff(
    registration_staff,
):
    assert registration_staff.post("/api/agents", {"name": "missing-csrf"}).status_code == 403
    assert (
        register(
            registration_staff,
            "invalid-bearer",
            HTTP_AUTHORIZATION="Bearer invalid",
        ).status_code
        == 401
    )
    assert not Agent.objects.filter(name__in=["missing-csrf", "invalid-bearer"]).exists()


def test_list_is_paginated_credential_free_and_has_no_mutation_endpoints(registration_staff):
    first = register(registration_staff, "a-first").json()
    register(registration_staff, "z-last")
    response = registration_staff.get("/api/agents?limit=1&offset=1")
    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert response.json()["items"][0]["name"] == "z-last"
    assert set(response.json()["items"][0]) == {
        "id",
        "name",
        "is_active",
        "last_seen",
        "created_at",
    }
    assert registration_staff.get("/api/agents?limit=501").status_code == 400
    for method in ["get", "delete", "patch"]:
        assert getattr(registration_staff, method)(f"/api/agents/{first['id']}").status_code == 404


def test_registration_notification_never_contains_the_raw_key(
    registration_staff,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    notifications = []
    monkeypatch.setattr(
        "coordinator.signals.publish_change", lambda: notifications.append("changed")
    )
    with django_capture_on_commit_callbacks(execute=True):
        created = register(registration_staff, "notified").json()
    assert notifications == ["changed"]
    assert created["api_key"] not in str(notifications)


@pytest.mark.django_db(transaction=True)
def test_concurrent_registration_returns_one_key_without_replacing_the_winner(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("Concurrent registration requires PostgreSQL")
    user = django_user_model.objects.create_user("concurrent-registrar", is_staff=True)
    clients = [Client(), Client()]
    for client in clients:
        client.force_login(user)
    barrier = Barrier(2)

    def attempt(client):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            response = client.post("/api/agents", {"name": "shared-name"})
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, clients))
    assert sorted(status for status, _ in results) == [201, 409]
    winner = next(body for status, body in results if status == 201)
    assert Agent.objects.filter(name="shared-name").count() == 1
    assert Agent.authenticate(winner["api_key"]).name == "shared-name"
