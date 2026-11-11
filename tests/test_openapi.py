"""Versioned API mount and OpenAPI contract (#39)."""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

SCHEMA = "/api/v1/schema?format=json"


@pytest.mark.django_db
def test_schema_requires_authentication(api_client):
    assert Client().get(SCHEMA).status_code == 401
    response = api_client.get(SCHEMA)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/vnd.oai.openapi+json")


@pytest.mark.django_db
def test_schema_is_readable_by_staff_sessions():
    user = User.objects.create_user("ops", password="pw", is_staff=True)
    client = Client()
    client.force_login(user)
    assert client.get(SCHEMA).status_code == 200


@pytest.mark.django_db
def test_schema_documents_only_the_versioned_mount(api_client):
    schema = api_client.get(SCHEMA).json()
    paths = schema["paths"]
    assert paths, "schema has no paths"
    assert all(path.startswith("/api/v1/") for path in paths)
    for expected in ["/api/v1/tasks", "/api/v1/tasks/{id}/claim", "/api/v1/overview"]:
        assert expected in paths
    assert "/api/v1/schema" not in paths
    assert schema["components"]["securitySchemes"]["agentKey"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Agent API key: `Authorization: Bearer <key>`.",
    }
    page = schema["components"]["schemas"]["PaginatedTaskList"]
    assert set(page["properties"]) == {"count", "items"}


@pytest.mark.django_db
def test_schema_generation_has_no_warnings(tmp_path):
    # Fails on any drf-spectacular warning so new views must document themselves.
    call_command("spectacular", "--validate", "--fail-on-warn", "--file", str(tmp_path / "s.yaml"))


@pytest.mark.django_db
def test_legacy_and_versioned_mounts_share_state(api_client, project):
    created = api_client.post(
        "/api/v1/tasks",
        {"title": "Versioned", "project": "coordinator"},
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    task_id = created.json()["id"]

    legacy = api_client.get(f"/api/tasks/{task_id}")
    assert legacy.status_code == 200
    assert legacy.json()["title"] == "Versioned"

    claimed = api_client.post(f"/api/tasks/{task_id}/claim", content_type="application/json")
    assert claimed.status_code == 200
    versioned = api_client.get(f"/api/v1/tasks/{task_id}").json()
    assert versioned["status"] == "in_progress"

    assert api_client.get("/api/overview").json() == api_client.get("/api/v1/overview").json()


@pytest.mark.django_db
def test_versioned_mount_keeps_authentication(api_client):
    assert Client().get("/api/v1/tasks").status_code == 401
    assert Client(HTTP_AUTHORIZATION="Bearer invalid").get("/api/v1/tasks").status_code == 401
    assert api_client.get("/api/v1/tasks").status_code == 200


def test_unnamespaced_route_names_still_resolve_to_legacy_paths():
    # MCP/bridge-facing code and existing tests reverse the plain names.
    assert reverse("task-list") == "/api/tasks"
    assert reverse("overview") == "/api/overview"
    assert reverse("v1:task-list") == "/api/v1/tasks"
    assert reverse("v1:overview") == "/api/v1/overview"
