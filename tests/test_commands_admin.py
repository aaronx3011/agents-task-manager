from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from coordinator.admin import TaskAdmin
from coordinator.models import Task
from coordinator.services import TaskError

from .test_api import create, post


@pytest.mark.django_db
def test_reaper_releases_only_stale_in_progress_claims(api_client, project):
    stale = create(api_client, "Stale")
    fresh = create(api_client, "Fresh")
    blocked = create(api_client, "Blocked")
    for task in [stale, fresh, blocked]:
        assert post(api_client, f"tasks/{task['id']}/claim").status_code == 200
    post(api_client, f"tasks/{blocked['id']}/status", {"status": "blocked", "reason": "Approval"})
    Task.objects.filter(pk__in=[stale["id"], blocked["id"]]).update(
        heartbeat_at=timezone.now() - timedelta(minutes=16)
    )
    output = StringIO()
    call_command("release_stale_tasks", stdout=output)
    assert "Released 1 stale task(s)" in output.getvalue()
    stale_detail = api_client.get(f"/api/tasks/{stale['id']}").json()
    assert stale_detail["status"] == "pending"
    assert stale_detail["claimed_by"] is None
    assert stale_detail["claimed_at"] is None and stale_detail["heartbeat_at"] is None
    assert stale_detail["recent_events"][0]["event_type"] == "released"
    assert stale_detail["recent_events"][0]["agent"] == "alpha"
    assert api_client.get(f"/api/tasks/{fresh['id']}").json()["status"] == "in_progress"
    assert api_client.get(f"/api/tasks/{blocked['id']}").json()["status"] == "blocked"
    call_command("release_stale_tasks", stdout=StringIO())
    assert len(api_client.get(f"/api/tasks/{stale['id']}").json()["recent_events"]) == 3
    with pytest.raises(CommandError, match="at least 1"):
        call_command("release_stale_tasks", minutes=0)


@pytest.mark.django_db
def test_create_agent_requires_explicit_rotation():
    output = StringIO()
    call_command("create_agent", "operator", stdout=output)
    original_key = output.getvalue().split(": ")[-1].strip()
    original = Client(HTTP_AUTHORIZATION=f"Bearer {original_key}")
    assert original.get("/api/tasks").status_code == 200
    with pytest.raises(CommandError, match="--rotate"):
        call_command("create_agent", "operator", stdout=StringIO())
    output = StringIO()
    call_command("create_agent", "operator", rotate=True, stdout=output)
    new_key = output.getvalue().split(": ")[-1].strip()
    assert original.get("/api/tasks").status_code == 401
    assert Client(HTTP_AUTHORIZATION=f"Bearer {new_key}").get("/api/tasks").status_code == 200


def admin_payload(project, **kwargs):
    return {
        "title": "Human task",
        "project": project.pk,
        "status": "pending",
        "priority": 100,
        "description": "Human acceptance criteria",
        "blocked_reason": "",
        "result_summary": "",
        "events-TOTAL_FORMS": 0,
        "events-INITIAL_FORMS": 0,
        "events-MIN_NUM_FORMS": 0,
        "events-MAX_NUM_FORMS": 1000,
        "_save": "Save",
        **kwargs,
    }


@pytest.fixture
def human_admin(client, db):
    user = get_user_model().objects.create_superuser("human", "human@example.com", "test-only")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_admin_task_create_override_and_dependency_changes_are_audited(
    human_admin,
    api_client,
    project,
):
    created = human_admin.post("/admin/coordinator/task/add/", admin_payload(project))
    assert created.status_code == 302, created.content
    task = api_client.get("/api/tasks").json()["items"][0]
    detail = api_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["recent_events"][0]["event_type"] == "created"
    assert "Human admin human" in detail["recent_events"][0]["message"]
    post(api_client, f"tasks/{task['id']}/claim")
    prerequisite = create(api_client, "Admin prerequisite")
    changed = human_admin.post(
        f"/admin/coordinator/task/{task['id']}/change/",
        admin_payload(
            project, status="done", result_summary="Human verified", depends_on=[prerequisite["id"]]
        ),
    )
    assert changed.status_code == 302, changed.content
    detail = api_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "done" and detail["claimed_by"] is None
    assert detail["depends_on"] == [prerequisite["id"]]
    assert "depends_on" in detail["recent_events"][0]["message"]
    assert "status" in detail["recent_events"][0]["message"]
    event = detail["recent_events"][0]["id"]
    assert human_admin.post(f"/admin/coordinator/taskevent/{event}/delete/").status_code == 403
    assert human_admin.post(f"/admin/coordinator/task/{task['id']}/delete/").status_code == 403


@pytest.mark.django_db
def test_admin_cannot_create_dependency_cycle(human_admin, api_client, project):
    first = create(api_client, "First")
    second = create(api_client, "Second", depends_on=[first["id"]])
    response = human_admin.post(
        f"/admin/coordinator/task/{first['id']}/change/",
        admin_payload(project, depends_on=[second["id"]]),
    )
    assert response.status_code == 200
    assert "Dependencies must not contain a cycle" in response.content.decode()
    assert api_client.get(f"/api/tasks/{first['id']}").json()["depends_on"] == []


@pytest.mark.django_db
@pytest.mark.parametrize("live_update", ["claim", "heartbeat"])
def test_admin_edit_preserves_claim_metadata_changed_after_form_load(
    api_client,
    project,
    live_update,
):
    task = create(api_client)
    task_id = task["id"]
    if live_update == "heartbeat":
        post(api_client, f"tasks/{task_id}/claim")
        Task.objects.filter(pk=task_id).update(
            heartbeat_at=timezone.now() - timedelta(minutes=10),
        )
    # The admin save hook may receive an instance loaded before another HTTP mutation.
    stale_form_instance = Task.objects.get(pk=task_id)
    assert post(api_client, f"tasks/{task_id}/{live_update}").status_code == 200
    live = api_client.get(f"/api/tasks/{task_id}").json()
    stale_form_instance.title = "Clarified title"
    request = SimpleNamespace(user=SimpleNamespace(get_username=lambda: "human"))
    form = SimpleNamespace(changed_data=["title"], cleaned_data={"depends_on": []})
    TaskAdmin(Task, admin.site).save_model(request, stale_form_instance, form, change=True)
    current = api_client.get(f"/api/tasks/{task_id}").json()
    assert current["title"] == "Clarified title"
    assert current["status"] == "in_progress"
    assert current["claimed_by"] == "alpha"
    assert current["claimed_at"] == live["claimed_at"]
    assert current["heartbeat_at"] == live["heartbeat_at"]
    assert current["recent_events"][0]["message"] == "Human admin human: updated title"


def test_health_probe_works_with_https_redirects(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    assert client.get("/healthz").status_code == 200
    assert client.get("/admin/").status_code == 301


@pytest.mark.django_db
def test_admin_edit_cannot_clear_summary_after_concurrent_review_transition(api_client, project):
    task = create(api_client)
    task_id = task["id"]
    post(api_client, f"tasks/{task_id}/claim")
    post(
        api_client,
        f"tasks/{task_id}/status",
        {
            "status": "in_progress",
            "result_summary": "Draft progress",
        },
    )
    stale_form_instance = Task.objects.get(pk=task_id)
    stale_form_instance.result_summary = ""
    assert (
        post(
            api_client,
            f"tasks/{task_id}/status",
            {
                "status": "review",
                "result_summary": "Ready for review",
            },
        ).status_code
        == 200
    )
    request = SimpleNamespace(user=SimpleNamespace(get_username=lambda: "human"))
    form = SimpleNamespace(changed_data=["result_summary"], cleaned_data={"depends_on": []})
    with pytest.raises(TaskError, match="require a result summary"):
        TaskAdmin(Task, admin.site).save_model(request, stale_form_instance, form, change=True)
    current = api_client.get(f"/api/tasks/{task_id}").json()
    assert current["status"] == "review"
    assert current["result_summary"] == "Ready for review"
    assert current["recent_events"][0]["event_type"] == "status_changed"
