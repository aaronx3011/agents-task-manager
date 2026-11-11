"""Dashboard read models on DRF under /api/v1/dashboard/ (#40)."""

import pytest
from django.test import Client

from .test_api import create, post

STATE = "/api/v1/dashboard/state"
WORKSPACE = "/api/v1/dashboard/workspace"
SCHEMA = "/api/v1/schema?format=json"


@pytest.fixture
def staff(django_user_model):
    client = Client()
    client.force_login(django_user_model.objects.create_user("observer", is_staff=True))
    return client


@pytest.fixture
def board(api_client, project):
    tasks = [create(api_client, f"Task {i}", priority=i) for i in range(4)]
    post(api_client, f"tasks/{tasks[0]['id']}/claim")
    post(api_client, f"tasks/{tasks[1]['id']}/claim")
    post(api_client, f"tasks/{tasks[1]['id']}/status", {"status": "done", "result_summary": "ok"})
    return tasks


def without_clock(payload):
    return {key: value for key, value in payload.items() if key != "server_time"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("legacy", "versioned", "params"),
    [
        ("/dashboard/state/", STATE, {}),
        ("/dashboard/state/", STATE, {"project": "coordinator"}),
        ("/dashboard/workspace/", WORKSPACE, {}),
        ("/dashboard/workspace/", WORKSPACE, {"status": "done"}),
        ("/dashboard/workspace/", WORKSPACE, {"claimed_by": "alpha", "q": "Task"}),
    ],
)
def test_legacy_and_versioned_payloads_are_identical(staff, board, legacy, versioned, params):
    old = staff.get(legacy, params)
    new = staff.get(versioned, params)
    assert old.status_code == new.status_code == 200
    assert without_clock(old.json()) == without_clock(new.json())
    assert "no-store" in new.headers["Cache-Control"]


@pytest.mark.django_db
def test_state_payload_shape(staff, board):
    state = staff.get(STATE).json()
    assert state["counts"]["in_progress"] == 1 and state["counts"]["done"] == 1
    assert state["total_tasks"] == state["displayed_tasks"] == 4
    assert state["tasks"][0]["claimed_by"] == "alpha"
    assert state["projects"][0]["counts"]["pending"] == 2
    assert state["events"][0]["agent"] == "alpha"
    assert staff.get(STATE, {"project": "unknown"}).status_code == 404


@pytest.mark.django_db
def test_workspace_payload_shape(staff, board):
    workspace = staff.get(WORKSPACE).json()
    assert workspace["total"] == 4 and workspace["ready_count"] == 2
    alpha = next(agent for agent in workspace["agents"] if agent["name"] == "alpha")
    assert [item["id"] for item in alpha["assignments"]] == [board[0]["id"]]
    assert set(alpha["assignments"][0]) == {"id", "title", "status", "heartbeat_at"}
    invalid = staff.get(WORKSPACE, {"status": "wrong"})
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": {"status": "Invalid task status"}}


@pytest.mark.django_db
@pytest.mark.parametrize("path", [STATE, WORKSPACE])
def test_versioned_dashboard_is_for_signed_in_humans(path, api_client, django_user_model):
    assert Client().get(path).status_code in {401, 403}
    assert api_client.get(path).status_code == 403
    viewer = Client()
    user = django_user_model.objects.create_user("viewer")
    viewer.force_login(user)
    assert viewer.get(path).status_code == 200
    user.is_active = False
    user.save()
    assert viewer.get(path).status_code in {401, 403}


@pytest.mark.django_db
def test_schema_documents_dashboard_endpoints(api_client):
    schema = api_client.get(SCHEMA).json()
    state = schema["paths"]["/api/v1/dashboard/state"]["get"]
    workspace = schema["paths"]["/api/v1/dashboard/workspace"]["get"]
    assert state["operationId"] == "dashboard_state"
    assert workspace["operationId"] == "dashboard_workspace"
    assert "limit" not in {param["name"] for param in workspace["parameters"]}
    components = schema["components"]["schemas"]
    assert {"DashboardState", "WorkspaceState", "StatusCounts"} <= set(components)
    assert set(components["StatusCounts"]["properties"]) == {
        "pending",
        "in_progress",
        "blocked",
        "review",
        "done",
        "cancelled",
    }
