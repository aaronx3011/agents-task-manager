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
from tests.test_planning import sync
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
        plan and (plan.revision, plan.playing, plan.pause_reason),
    )


def test_policy_advertises_queue_actions_without_auth_fields():
    names = {item["function"]["name"] for item in tool_configs()}
    assert {"get_queue", "launch_task", "play_queue"} <= names
    assert "durable ordered queue" in PROMPT and "next sync" in PROMPT
    assert "worker/session dispatch" not in PROMPT
    assert POLICY_VERSION.endswith(".2")


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
