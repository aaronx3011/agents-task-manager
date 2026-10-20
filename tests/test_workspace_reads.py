import pytest
from django.test import Client

from coordinator.models import Project, Task

from .test_api import create, post


@pytest.fixture
def staff(django_user_model):
    user = django_user_model.objects.create_user("read-only-observer", is_staff=True)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_shared_search_filters_ordering_and_counts(api_client, project, staff):
    first = create(
        api_client, "Prepare contract", description="Acceptance: validate OAuth", priority=50
    )
    second = create(
        api_client, "Document workflow", description="Acceptance: inspect signatures", priority=10
    )
    assert api_client.get("/api/tasks", {"q": "oauth"}).json()["items"][0]["id"] == first["id"]
    assert api_client.get("/api/tasks", {"q": f"#{second['id']}"}).json()["count"] == 1
    assert [
        task["id"] for task in api_client.get("/api/tasks", {"ordering": "title"}).json()["items"]
    ] == [second["id"], first["id"]]
    assert api_client.get("/api/tasks", {"ordering": "password"}).status_code == 400
    assert api_client.get("/api/tasks", {"q": "x" * 301}).status_code == 400
    state = staff.get("/dashboard/workspace/", {"q": "oauth"}).json()
    assert state["total"] == 1 and state["counts"]["pending"] == 1
    assert state["ready_count"] == 1
    assert state["events"][0]["task_id"] == first["id"]
    assert staff.get("/dashboard/workspace/", {"status": "wrong"}).status_code == 400
    assert Client().get("/dashboard/workspace/").status_code == 302


@pytest.mark.django_db
def test_presence_is_not_derived_from_filtered_task_pages(api_client, project, staff):
    task = create(api_client)
    post(api_client, f"tasks/{task['id']}/claim")
    state = staff.get("/dashboard/workspace/", {"status": "done"}).json()
    assert state["total"] == 0
    alpha = next(agent for agent in state["agents"] if agent["name"] == "alpha")
    assert alpha["assignments"][0]["id"] == task["id"]
    assert alpha["assignments"][0]["heartbeat_at"] is not None
    assert "api_key_hash" not in str(state) and "key_prefix" not in str(state)
    assert api_client.get("/api/tasks", {"claimed_by": "__unclaimed__"}).json()["count"] == 0
    other = create(api_client, "Unassigned")
    assert (
        api_client.get("/api/tasks", {"claimed_by": "__unclaimed__"}).json()["items"][0]["id"]
        == other["id"]
    )


@pytest.mark.django_db
def test_lane_pagination_keeps_exact_totals_and_dependency_progress(api_client, project, staff):
    prerequisite = create(api_client, "Prerequisite")
    dependent = create(api_client, "Waiting", depends_on=[prerequisite["id"]])
    Task.objects.bulk_create([Task(project=project, title=f"Work {index}") for index in range(43)])
    first = api_client.get("/api/tasks", {"status": "pending", "limit": 20}).json()
    second = api_client.get("/api/tasks", {"status": "pending", "limit": 20, "offset": 20}).json()
    assert first["count"] == second["count"] == 45
    assert not {task["id"] for task in first["items"]} & {task["id"] for task in second["items"]}
    assert staff.get("/dashboard/workspace/").json()["counts"]["pending"] == 45
    detail = api_client.get(f"/api/tasks/{dependent['id']}").json()
    assert detail["dependency_progress"] == {"done": 0, "total": 1}
    assert not detail["is_ready"]
    # Detail is canonical even if supplied filters would exclude the task from a list.
    assert api_client.get(f"/api/tasks/{dependent['id']}", {"status": "done"}).status_code == 200
    Project.objects.create(slug="empty-project", name="Empty")
    assert staff.get("/dashboard/workspace/", {"project": "empty-project"}).json()["total"] == 0


@pytest.mark.django_db
def test_audit_cursor_paging_is_stable_when_new_events_arrive(api_client, project):
    task = create(api_client)
    for index in range(25):
        post(api_client, f"tasks/{task['id']}/notes", {"message": f"Progress {index}"})
    first = api_client.get(f"/api/tasks/{task['id']}/events").json()
    assert len(first["items"]) == 20 and first["next_before"] is not None
    post(api_client, f"tasks/{task['id']}/notes", {"message": "Newer while paging"})
    second = api_client.get(
        f"/api/tasks/{task['id']}/events", {"before": first["next_before"]}
    ).json()
    assert len(second["items"]) == 6 and second["next_before"] is None
    assert not {event["id"] for event in first["items"]} & {
        event["id"] for event in second["items"]
    }
    assert all(event["message"] != "Newer while paging" for event in second["items"])
    assert (
        api_client.get(f"/api/tasks/{task['id']}/events", {"before": "invalid"}).status_code == 400
    )
    assert api_client.get("/api/tasks/999999/events").status_code == 404
