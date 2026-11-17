"""PM router queue actions: launch a task, start/play a queue (#333)."""

# ruff: noqa: F811  (shared voice fixtures are imported from test_voice)

from datetime import timedelta

import pytest
from django.utils import timezone

from coordinator import services
from coordinator.models import (
    Agent,
    PlanningCard,
    PlanningJob,
    ProjectPlan,
    Task,
    TaskEvent,
    VoiceToolResult,
    WorkerCommand,
)
from coordinator.voice_policy import POLICY_VERSION, PROMPT, tool_configs
from tests.test_planning import accept, sync
from tests.test_voice import call, chat, completion, owner, tool_call, voice  # noqa: F401

pytestmark = pytest.mark.django_db

MODEL = {"providerID": "test", "modelID": "model"}
OTHER = {"providerID": "test", "modelID": "big/model"}


@pytest.fixture
def worker(project, agent_clients):
    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.default_model = MODEL
    worker.model_catalog = [MODEL, OTHER]
    worker.save()
    ProjectPlan.objects.create(project=project, default_worker=worker, directory="/workspace")
    return worker


def snapshot():
    """Every row a refused action must leave untouched."""
    plan = ProjectPlan.objects.order_by("pk").first()
    return (
        Task.objects.count(),
        TaskEvent.objects.count(),
        PlanningJob.objects.count(),
        WorkerCommand.objects.count(),
        list(PlanningCard.objects.values_list("pk", "worker_id", "model")),
        list(PlanningJob.objects.values_list("pk", "status", "worker_id", "snapshot", "detail")),
        plan and (plan.revision, plan.playing, plan.pause_reason),
    )


def test_policy_advertises_queue_actions_without_auth_fields():
    names = {item["function"]["name"] for item in tool_configs()}
    assert {
        "get_queue",
        "launch_task",
        "play_queue",
        "remove_queued_job",
        "retarget_queued_job",
        "pause_queue",
        "stop_queue",
    } <= names
    assert "durable ordered queue" in PROMPT and "next sync" in PROMPT
    assert "worker/session dispatch" not in PROMPT
    assert POLICY_VERSION.endswith(".4")


def test_launch_new_task_queues_plays_and_dispatches_on_sync(
    voice, project, worker, owner, agent_clients
):
    client, grant = voice
    response = call(
        grant,
        "launch_task",
        {"project": project.slug, "title": "Fix mobile login", "description": "Works on iOS"},
    )
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    task = Task.objects.get()
    assert result["created"] is True and result["task"] == task.pk
    assert result["worker"] == "worker-a" and result["model"] == "test/model"
    assert result["directory"] == "/workspace" and result["jobs_ahead"] == 0
    assert result["job_status"] == "queued" and result["queue_playing"] is True
    assert "Not started or completed" in result["state"]
    # Accepted is not executed: nothing is claimed or dispatched until worker sync.
    assert task.status == "pending" and not WorkerCommand.objects.exists()
    note = task.events.filter(event_type=TaskEvent.Type.NOTE).get()
    assert note.actor_user == owner and "PM router" in note.message
    # Dashboard sees the same queue.
    board = client.get(f"/api/projects/{project.slug}/planning").json()
    assert board["playing"] is True and board["jobs"][0]["status"] == "queued"
    # Replaying the identical request does not queue twice.
    again = call(
        grant,
        "launch_task",
        {"project": project.slug, "title": "Fix mobile login", "description": "Works on iOS"},
    )
    assert again.json()["replayed"] is True and PlanningJob.objects.count() == 1
    # The real existing dispatch path starts it.
    commands = sync(agent_clients[0]).json()["commands"]
    assert [c["action"] for c in commands] == ["create"]
    assert commands[0]["payload"]["model"] == MODEL
    task.refresh_from_db()
    assert task.status == "in_progress" and task.claimed_by.name == "alpha"
    queue = call(grant, "get_queue", {"project": project.slug}).json()["result"]
    assert queue["state"] == "playing" and queue["jobs"][0]["status"] == "running"
    assert queue["jobs"][0]["task_status"] == "in_progress"
    assert queue["workers"][0]["online"] is True
    assert "test/big/model" in queue["workers"][0]["models"]


def test_launch_existing_task_with_named_model_and_queue_position(voice, project, worker):
    _, grant = voice
    first = services.create_task(None, project, "First")
    second = services.create_task(None, project, "Second")
    assert call(grant, "launch_task", {"id": first.pk}).status_code == 200
    response = call(grant, "launch_task", {"id": second.pk, "model": "test/big/model"})
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert result["model"] == "test/big/model" and result["jobs_ahead"] == 1
    assert PlanningCard.objects.get(task=second).model == OTHER
    job = PlanningJob.objects.get(card__task=second)
    assert job.snapshot["model"] == OTHER


def test_queue_without_start_does_not_play(voice, project, worker):
    _, grant = voice
    task = services.create_task(None, project, "Later")
    result = call(grant, "launch_task", {"id": task.pk, "start": False}).json()["result"]
    assert result["queue_playing"] is False and "nothing will start" in result["state"]
    assert ProjectPlan.objects.get().playing is False


def test_play_queue_resumes_paused_queue_and_is_idempotent(voice, project, worker):
    _, grant = voice
    task = services.create_task(None, project, "Paused work")
    call(grant, "launch_task", {"id": task.pk, "start": False})
    plan = ProjectPlan.objects.get()
    plan.pause_reason = "Paused by operator"
    plan.save()
    response = call(grant, "play_queue", {"project": project.slug})
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert result["state"].startswith("queue playing") and result["playing"] is True
    assert result["cleared_pause_reason"] == "Paused by operator"
    assert result["jobs"][0]["task"] == task.pk and result["jobs"][0]["worker"] == "worker-a"
    again = call(grant, "play_queue", {"project": project.slug}).json()
    assert again["replayed"] is False and again["result"]["already_playing"] is True
    assert VoiceToolResult.objects.filter(tool="play_queue").exists()


@pytest.mark.parametrize(
    "case",
    ["offline", "unknown_model", "no_catalog", "unknown_worker", "owned", "dependency", "new"],
)
def test_launch_refusals_leave_no_partial_mutation(voice, project, worker, case):
    _, grant = voice
    task = services.create_task(None, project, "Candidate")
    payload = {"id": task.pk}
    if case == "offline":
        worker.last_sync_at = timezone.now() - timedelta(hours=1)
        worker.save()
    elif case == "unknown_model":
        payload["model"] = "test/missing"
    elif case == "no_catalog":
        worker.model_catalog = []
        worker.save()
        payload["model"] = "test/model"
    elif case == "unknown_worker":
        payload["worker"] = "ghost"
    elif case == "owned":
        services.claim_task(worker.agent, task.pk)
    elif case == "dependency":
        task = services.create_task(None, project, "Dependent", depends_on=[task.pk])
        payload = {"id": task.pk}
    elif case == "new":
        # A brand-new task must not be left behind when launch is refused.
        worker.connected = False
        worker.save()
        payload = {"project": project.slug, "title": "Should not persist"}
    before = snapshot()
    response = call(grant, "launch_task", payload)
    assert response.status_code in {400, 409}, response.content
    assert snapshot() == before
    assert not Task.objects.filter(title="Should not persist").exists()
    message = str(response.json())
    expected = {
        "offline": "offline",
        "unknown_model": "not in worker",
        "no_catalog": "has not reported a model catalog",
        "unknown_worker": "Unknown worker",
        "owned": "already in_progress",
        "dependency": "dependencies",
        "new": "offline",
    }[case]
    assert expected in message


def queue_one(grant, project):
    task = services.create_task(None, project, "Queue control candidate")
    response = call(grant, "launch_task", {"id": task.pk, "start": False})
    assert response.status_code == 200, response.content
    return task, PlanningJob.objects.get(pk=response.json()["result"]["job"])


def test_remove_queued_job_keeps_task_and_updates_board_audit(voice, project, worker, owner):
    client, grant = voice
    task, job = queue_one(grant, project)
    response = call(grant, "remove_queued_job", {"project": project.slug, "job_id": job.pk})
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert result["affected"]["job"] == job.pk
    assert result["affected"]["task"] == task.pk
    assert result["affected"]["worker"] == worker.name
    assert result["affected"]["model"] == "test/model"
    assert result["affected"]["directory"] == "/workspace"
    assert result["affected"]["status"] == "cancelled"
    assert result["active_count"] == 0
    task.refresh_from_db()
    assert task.status == "pending"
    assert task.events.filter(actor_user=owner, message__contains=f"job #{job.pk}").exists()
    board = client.get(f"/api/projects/{project.slug}/planning").json()
    assert board["jobs"][0]["status"] == "cancelled"
    # Removal does not make the card terminal: it can be queued again in another conversation.
    result = services.voice_launch_task(task, owner.username, model="test/big/model", start=False)
    assert result["job"] != job.pk and result["model"] == "test/big/model"


def test_retarget_changes_real_dispatch_and_preserves_order_context(
    voice, project, worker, agent_clients
):
    _, grant = voice
    task, job = queue_one(grant, project)
    other = services.provision_worker("worker-b", Agent.objects.get(name="beta"))
    other.connected = other.stream_connected = True
    other.last_sync_at = timezone.now()
    other.model_catalog = [OTHER]
    other.default_model = OTHER
    other.agent_catalog = [{"name": "build"}]
    other.save()
    frozen, position = job.snapshot.copy(), job.position
    response = call(
        grant,
        "retarget_queued_job",
        {
            "project": project.slug,
            "job_id": job.pk,
            "worker": other.name,
            "model": "test/big/model",
            "agent": "build",
        },
    )
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert result["affected"]["worker"] == other.name
    assert result["affected"]["agent"] == "build"
    assert "still queued" in result["state"]
    job.refresh_from_db()
    assert job.position == position and job.status == "queued"
    assert job.snapshot["context"] == frozen["context"]
    assert job.snapshot["directory"] == frozen["directory"]
    assert job.snapshot["model"] == OTHER
    assert call(grant, "play_queue", {"project": project.slug}).status_code == 200
    assert sync(agent_clients[0]).json()["commands"] == []
    commands = sync(agent_clients[1], worker="worker-b").json()["commands"]
    assert [c["action"] for c in commands] == ["create"]
    assert commands[0]["payload"]["model"] == OTHER
    task.refresh_from_db()
    assert task.claimed_by == other.agent


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "foreign_project",
        "running",
        "offline",
        "unknown_worker",
        "unknown_model",
        "unknown_agent",
        "missing_change",
    ],
)
def test_retarget_refusals_are_atomic(voice, project, worker, case):
    from coordinator.models import Project

    _, grant = voice
    _, job = queue_one(grant, project)
    payload = {"project": project.slug, "job_id": job.pk, "model": "test/big/model"}
    if case == "unknown":
        payload["job_id"] = job.pk + 1000
    elif case == "foreign_project":
        payload["project"] = Project.objects.create(slug="foreign", name="Foreign").slug
    elif case == "running":
        job.status = "running"
        job.save()
    elif case == "offline":
        worker.connected = False
        worker.save()
    elif case == "unknown_worker":
        payload["worker"] = "ghost"
    elif case == "unknown_model":
        payload["model"] = "test/ghost"
    elif case == "unknown_agent":
        payload["agent"] = "ghost"
    else:
        payload.pop("model")
    before = snapshot()
    response = call(grant, "retarget_queued_job", payload)
    assert response.status_code in {400, 404, 409}, response.content
    assert snapshot() == before


def test_cancel_task_invalidates_job_and_legacy_stale_job_is_removable(
    voice, project, worker, agent_clients
):
    _, grant = voice
    task, job = queue_one(grant, project)
    response = call(grant, "update_task_status", {"id": task.pk, "status": "cancelled"})
    assert response.status_code == 200, response.content
    job.refresh_from_db()
    assert job.status == "cancelled" and job.completed_at
    assert task.events.filter(message__contains="queued execution invalidated").exists()
    # Simulate a legacy stale job. The removal path does not check task lifecycle.
    PlanningJob.objects.filter(pk=job.pk).update(status="queued")
    response = call(grant, "remove_queued_job", {"project": project.slug, "job_id": job.pk})
    assert response.status_code == 200, response.content
    assert response.json()["result"]["affected"]["task_status"] == "cancelled"
    assert sync(agent_clients[0]).json()["commands"] == []


def test_stale_cancelled_job_never_dispatches_even_if_playing(
    voice, project, worker, agent_clients
):
    _, grant = voice
    task, job = queue_one(grant, project)
    Task.objects.filter(pk=task.pk).update(status="cancelled")
    ProjectPlan.objects.filter(project=project).update(playing=True)
    assert sync(agent_clients[0]).json()["commands"] == []
    job.refresh_from_db()
    assert job.status == "cancelled"
    assert not WorkerCommand.objects.exists()


@pytest.mark.parametrize("operation", ["pause_queue", "stop_queue"])
def test_pause_stop_waiting_queue_no_dispatch(voice, project, worker, agent_clients, operation):
    _, grant = voice
    _, job = queue_one(grant, project)
    assert call(grant, "play_queue", {"project": project.slug}).status_code == 200
    response = call(grant, operation, {"project": project.slug})
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert result["playing"] is False and result["affected"]["job"] == job.pk
    if operation == "stop_queue":
        assert result["interrupt"] is None and "no interrupt was queued" in result["state"]
    assert sync(agent_clients[0]).json()["commands"] == []
    # Each invocation checks live state rather than returning an obsolete cached pause.
    call(grant, "play_queue", {"project": project.slug})
    again = call(grant, operation, {"project": project.slug}).json()
    assert again["replayed"] is False and again["result"]["playing"] is False


@pytest.mark.parametrize("operation", ["pause_queue", "stop_queue"])
def test_empty_queue_controls_refused(voice, project, worker, operation):
    _, grant = voice
    before = snapshot()
    response = call(grant, operation, {"project": project.slug})
    assert response.status_code == 409 and "empty" in str(response.json())
    assert snapshot() == before


def test_running_job_remove_retarget_refused_and_stop_uses_durable_interrupt(
    voice, project, worker, agent_clients
):
    _, grant = voice
    task, job = queue_one(grant, project)
    call(grant, "play_queue", {"project": project.slug})
    create = sync(agent_clients[0]).json()["commands"][0]
    accept(agent_clients[0], create)
    before = snapshot()
    for operation in ["remove_queued_job", "retarget_queued_job"]:
        payload = {"project": project.slug, "job_id": job.pk}
        if operation == "retarget_queued_job":
            payload["model"] = "test/big/model"
        response = call(grant, operation, payload)
        assert response.status_code == 409 and "running" in str(response.json())
        assert snapshot() == before
    paused = call(grant, "pause_queue", {"project": project.slug})
    assert (
        paused.status_code == 200
        and "current work may continue" in paused.json()["result"]["state"]
    )
    job.refresh_from_db()
    assert job.status == "running"
    response = call(grant, "stop_queue", {"project": project.slug})
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert "Interruption is not confirmed" in result["state"]
    assert result["affected"]["worker"] == worker.name and result["affected"]["task"] == task.pk
    task.refresh_from_db()
    assert task.status == "in_progress" and task.claimed_by == worker.agent
    job.refresh_from_db()
    assert job.status == "failed" and job.stop_command.action == "interrupt"
    assert result["interrupt"] == {"id": job.stop_command_id, "status": "queued"}
    assert [c["action"] for c in sync(agent_clients[0]).json()["commands"]] == ["interrupt"]
    assert sync(agent_clients[0]).json()["commands"] == []


def test_stop_refusal_inflight_and_offline_are_atomic(voice, project, worker, agent_clients):
    _, grant = voice
    queue_one(grant, project)
    call(grant, "play_queue", {"project": project.slug})
    create = sync(agent_clients[0]).json()["commands"][0]
    before = snapshot()
    response = call(grant, "stop_queue", {"project": project.slug})
    assert response.status_code == 409 and "in flight" in str(response.json())
    assert snapshot() == before
    accept(agent_clients[0], create)
    worker.connected = False
    worker.save()
    before = snapshot()
    response = call(grant, "stop_queue", {"project": project.slug})
    assert response.status_code == 409 and "offline" in str(response.json())
    assert snapshot() == before


@pytest.mark.parametrize(
    "operation", ["remove_queued_job", "retarget_queued_job", "pause_queue", "stop_queue"]
)
def test_spoken_controls_receive_real_state(voice, project, worker, monkeypatch, operation):
    _, grant = voice
    task = services.create_task(None, project, "Spoken queue control")
    launched = services.voice_launch_task(task, "pm-owner", start=False)
    job = PlanningJob.objects.get(pk=launched["job"])
    payload = {"project": project.slug}
    if "queued_job" in operation:
        payload["job_id"] = job.pk
    if operation == "retarget_queued_job":
        payload["model"] = "test/big/model"
    replies = iter(
        [
            completion(calls=[tool_call(operation, payload)]),
            completion("The requested queue change was accepted; the task has not completed."),
        ]
    )
    messages = []

    def provider(method, url, **kwargs):
        messages.append(kwargs["json"]["messages"])
        return next(replies)

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    response = chat(grant, "Apply the queue change")
    assert response.status_code == 200, response.content
    assert response.json()["events"] == [{"tool": operation, "is_error": False}]
    import json

    result = json.loads(messages[-1][-1]["content"])["result"]
    assert result["affected"]["job"] == job.pk
    assert result["affected"]["worker"] == worker.name
    assert result["affected"]["model"] == (
        "test/big/model" if operation == "retarget_queued_job" else "test/model"
    )
    assert result["affected"]["directory"] == "/workspace"


def test_admin_cancellation_invalidates_waiting_job(voice, project, worker):
    _, grant = voice
    task, job = queue_one(grant, project)
    task.status = "cancelled"
    services.admin_save_task(task, {"status"}, [], "pm-owner")
    job.refresh_from_db()
    assert job.status == "cancelled"
    assert task.events.filter(message__contains=f"Queue job #{job.pk}").exists()


def test_migration_repairs_existing_cancelled_jobs_with_audit(voice, project, worker):
    import importlib
    from types import SimpleNamespace

    from django.apps import apps
    from django.db import connection

    _, grant = voice
    task, job = queue_one(grant, project)
    Task.objects.filter(pk=task.pk).update(status="cancelled")
    revision = ProjectPlan.objects.get().revision
    migration = importlib.import_module("coordinator.migrations.0021_cancel_stale_queued_jobs")
    migration.invalidate_cancelled_tasks(apps, SimpleNamespace(connection=connection))
    job.refresh_from_db()
    assert job.status == "cancelled" and job.completed_at
    assert ProjectPlan.objects.get().revision == revision + 1
    assert task.events.filter(message__contains="migration 0021").exists()


@pytest.mark.django_db(transaction=True)
def test_cancel_plan_lock_blocks_dispatch_until_invalidation(project, worker):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection, transaction

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL verifies cancellation/dispatch plan locks")
    task = services.create_task(None, project, "Cancelled before dispatch")
    launched = services.voice_launch_task(task, "planner", start=True)

    def tick():
        close_old_connections()
        try:
            with transaction.atomic():
                services.tick_planning_queues(worker)
        finally:
            close_old_connections()

    with transaction.atomic():
        services._lock_task_plans(task.pk)
        # Worker sync uses SKIP LOCKED; a held cancellation lock prevents any launch.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(tick).result(timeout=10)
        assert not WorkerCommand.objects.exists()
        services.update_status(None, task.pk, "cancelled", human_actor="planner")
    tick()
    assert PlanningJob.objects.get(pk=launched["job"]).status == "cancelled"
    assert not WorkerCommand.objects.exists()


@pytest.mark.parametrize(
    "operation", ["remove_queued_job", "retarget_queued_job", "pause_queue", "stop_queue"]
)
def test_queue_controls_recheck_owner_capability_and_origin(
    voice, project, worker, owner, operation
):
    from django.test import Client

    from accounts.models import UserProfile

    _, grant = voice
    _, job = queue_one(grant, project)
    payload = {"project": project.slug}
    if "queued_job" in operation:
        payload["job_id"] = job.pk
    if operation == "retarget_queued_job":
        payload["model"] = "test/big/model"
    before = snapshot()
    assert Client().post(
        f"/api/v1/voice/tools/{operation}", payload, content_type="application/json"
    ).status_code in {401, 403}
    for origin in ["https://evil.example", "null", "http://["]:
        assert call(grant, operation, payload, HTTP_ORIGIN=origin).status_code == 403
    assert (
        call(
            grant, "get_queue", {"project": project.slug}, HTTP_ORIGIN="http://testserver"
        ).status_code
        == 200
    )
    UserProfile.objects.filter(user=owner).update(role="viewer")
    assert call(grant, operation, payload).status_code in {401, 403}
    assert snapshot() == before


def test_play_refusals_empty_queue_and_offline_worker(voice, project, worker):
    _, grant = voice
    empty = call(grant, "play_queue", {"project": project.slug})
    assert empty.status_code == 409 and "empty" in str(empty.json())
    task = services.create_task(None, project, "Waiting")
    call(grant, "launch_task", {"id": task.pk, "start": False})
    worker.stream_connected = False
    worker.save()
    before = snapshot()
    offline = call(grant, "play_queue", {"project": project.slug})
    assert offline.status_code == 409 and "offline" in str(offline.json())
    assert snapshot() == before and ProjectPlan.objects.get().playing is False


def test_launch_input_rejects_mixed_or_missing_target(voice, project, worker):
    _, grant = voice
    task = services.create_task(None, project, "Target")
    assert call(grant, "launch_task", {"id": task.pk, "title": "x"}).status_code == 400
    assert call(grant, "launch_task", {"project": project.slug}).status_code == 400
    assert call(grant, "launch_task", {"id": task.pk, "user": "x"}).status_code == 400


def test_viewer_or_foreign_token_cannot_launch(voice, project, worker):
    from django.contrib.auth import get_user_model
    from django.test import Client

    from accounts.models import UserProfile

    task = services.create_task(None, project, "Guarded")
    anonymous = Client().post(
        "/api/v1/voice/tools/launch_task",
        {"conversation_id": "conv_x", "id": task.pk},
        content_type="application/json",
    )
    assert anonymous.status_code in {401, 403}
    viewer = get_user_model().objects.create_user("viewer-q")
    UserProfile.objects.update_or_create(user=viewer, defaults={"role": "viewer"})
    browser = Client()
    browser.force_login(viewer)
    assert browser.post("/api/v1/voice/sessions").status_code == 403
    assert not PlanningJob.objects.exists()


def test_spoken_flow_reports_tool_errors_truthfully(voice, project, worker, monkeypatch):
    _, grant = voice
    worker.connected = False
    worker.save()
    seen = []
    replies = iter(
        [
            completion(calls=[tool_call("play_queue", {"project": project.slug})]),
            completion("The queue is empty, so nothing was started."),
        ]
    )

    def provider(method, url, **kwargs):
        seen.append(kwargs["json"]["messages"])
        return next(replies)

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    response = chat(grant, "Play the coordinator queue")
    assert response.status_code == 200, response.content
    assert response.json()["events"] == [{"tool": "play_queue", "is_error": True}]
    tool_message = seen[-1][-1]
    assert tool_message["role"] == "tool" and "empty" in tool_message["content"]
    assert seen[0][0]["content"] == PROMPT


def test_play_refused_while_failed_job_holds_queue(voice, project, worker, agent_clients):
    _, grant = voice
    first = services.create_task(None, project, "Fails")
    assert call(grant, "launch_task", {"id": first.pk}).status_code == 200
    sync(agent_clients[0])
    job = PlanningJob.objects.get(card__task=first)
    job.status = "failed"
    job.save()
    plan = ProjectPlan.objects.get()
    plan.playing = False
    plan.save()
    second = services.create_task(None, project, "Next")
    call(grant, "launch_task", {"id": second.pk, "start": False})
    before = snapshot()
    response = call(grant, "play_queue", {"project": project.slug})
    assert response.status_code == 409 and f"task #{first.pk}" in str(response.json())
    assert snapshot() == before
