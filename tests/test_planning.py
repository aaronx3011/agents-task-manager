"""Planning boundary and real sync-driven sequential orchestration."""

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, transaction
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, PlanningCard, PlanningJob, ProjectPlan, Task, WorkerCommand

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(project, agent_clients):
    staff = Client()
    staff.force_login(User.objects.create_user("planner", is_staff=True))
    agent = Agent.objects.get(name="alpha")
    worker = services.provision_worker("worker-a", agent)
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.default_model = {"providerID": "test", "modelID": "model"}
    worker.model_catalog = [worker.default_model]
    worker.save()
    plan = ProjectPlan.objects.create(
        project=project,
        default_worker=worker,
        directory="/workspace",
        context="Project architecture",
        instructions="Run relevant checks",
    )
    return staff, worker, plan


def mutate(staff, plan, operation, **values):
    plan.refresh_from_db()
    return staff.post(
        f"/api/projects/{plan.project.slug}/planning",
        {"revision": plan.revision, "operation": operation, **values},
        content_type="application/json",
    )


def card(plan, title="Build feature"):
    return PlanningCard.objects.create(
        plan=plan,
        title=title,
        description="Acceptance: works correctly",
        checklist=[{"text": "Verify", "done": False}],
        labels=["backend"],
    )


def sync(client, **values):
    return client.post(
        "/api/worker/sync",
        {
            "worker": "worker-a",
            "protocol_version": 1,
            "meta": {"connected": True, "streamConnected": True},
            "sessions": [],
            "updates": [],
            "events": [],
            "results": [],
            "pending": {"complete": True, "permissions": [], "questions": []},
            "cursor": {},
            **values,
        },
        content_type="application/json",
    )


def start(setup, client, count=2):
    staff, worker, plan = setup
    cards = [card(plan, f"Task {n}") for n in range(count)]
    for c in cards:
        response = mutate(staff, plan, "enqueue", card_id=c.pk, instructions="Implement carefully")
        assert response.status_code == 200, response.content
    assert mutate(staff, plan, "play").status_code == 200
    response = sync(client)
    assert response.status_code == 200, response.content
    command = response.json()["commands"][0]
    assert command["action"] == "create"
    return cards, command


def accept(client, command, sid="ses_planning"):
    response = sync(
        client,
        sessions=[{"id": sid, "directory": "/workspace"}],
        results=[
            {
                "commandID": command["id"],
                "status": "done",
                "result": {"id": sid},
            }
        ],
    )
    assert response.status_code == 200, response.content
    commands = response.json()["commands"]
    assert len(commands) == 1 and commands[0]["action"] == "prompt"
    prompt = commands[0]
    response = sync(
        client,
        results=[{"commandID": prompt["id"], "status": "done", "result": {"accepted": True}}],
    )
    assert response.status_code == 200, response.content
    assert response.json()["commands"] == []
    return prompt


def token(job):
    return re.search(r"token=([A-Za-z0-9_-]+)", job.prompt).group(1)


def test_sequential_sessions_completion_handoff_and_no_idle_advance(setup, agent_clients):
    staff, worker, plan = setup
    cards, command = start(setup, agent_clients[0])
    job = PlanningJob.objects.get(status="running")
    assert Task.objects.count() == 1
    prompt = accept(agent_clients[0], command)
    text = prompt["payload"]["text"]
    for expected in [
        "Project architecture",
        "Run relevant checks",
        "Acceptance:",
        "Verify",
        "Implement carefully",
    ]:
        assert expected in text
    assert prompt["payload"]["model"] == worker.default_model
    # Acceptance and idle snapshots never launch a second task.
    for _ in range(2):
        assert (
            sync(agent_clients[0], sessions=[{"id": "ses_planning", "status": "idle"}]).json()[
                "commands"
            ]
            == []
        )
    assert Task.objects.count() == 1
    # A different authenticated MCP identity can report only with this job capability.
    response = agent_clients[1].post(
        f"/api/planning/jobs/{job.pk}/report",
        {
            "token": token(job),
            "status": "done",
            "summary": "Backend ready; endpoint /login verified",
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    response = sync(agent_clients[0])
    assert response.status_code == 200, response.content
    assert len(response.json()["commands"]) == 1
    job.refresh_from_db()
    assert job.status == "completed"
    next_job = PlanningJob.objects.get(status="running")
    assert next_job.card_id == cards[1].pk
    assert "endpoint /login verified" in next_job.prompt
    assert Task.objects.count() == 2
    # Durable state: freshly loaded rows and the next sync retain the current job.
    assert sync(agent_clients[0]).json()["commands"] == []
    assert PlanningJob.objects.filter(status="running").count() == 1


def test_pause_finishes_current_without_advancing_and_settings_are_frozen(setup, agent_clients):
    staff, worker, plan = setup
    cards, command = start(setup, agent_clients[0])
    job = PlanningJob.objects.get(status="running")
    accept(agent_clients[0], command)
    assert mutate(staff, plan, "pause").status_code == 200
    ProjectPlan.objects.filter(pk=plan.pk).update(context="New architecture", directory="/new")
    services.update_status(worker.agent, job.attempt.task_id, "done", result_summary="Verified")
    assert sync(agent_clients[0]).json()["commands"] == []
    job.refresh_from_db()
    assert job.status == "completed"
    assert mutate(staff, plan, "play").status_code == 200
    response = sync(agent_clients[0])
    assert response.json()["commands"][0]["payload"]["directory"] == "/workspace"
    next_job = PlanningJob.objects.get(status="running")
    assert "Project architecture" in next_job.prompt and "New architecture" not in next_job.prompt


def test_question_pauses_queue_and_requires_explicit_resume(setup, agent_clients):
    staff, worker, plan = setup
    _, command = start(setup, agent_clients[0])
    accept(agent_clients[0], command)
    response = sync(
        agent_clients[0],
        pending={
            "complete": True,
            "permissions": [],
            "questions": [
                {
                    "id": "q1",
                    "sessionID": "ses_planning",
                    "questions": [
                        {
                            "header": "Choice",
                            "question": "Which?",
                            "options": [{"label": "A", "description": "A"}],
                        }
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200, response.content
    plan.refresh_from_db()
    assert not plan.playing and "input" in plan.pause_reason
    sync(agent_clients[0])
    assert not ProjectPlan.objects.get(pk=plan.pk).playing


def test_uncertain_and_stop_require_resolution_before_next_task(setup, agent_clients):
    staff, worker, plan = setup
    _, command = start(setup, agent_clients[0])
    response = sync(
        agent_clients[0],
        results=[
            {
                "commandID": command["id"],
                "status": "error",
                "error": "Delivery uncertain after restart",
            }
        ],
    )
    assert response.status_code == 200, response.content
    assert not response.json()["commands"]
    job = PlanningJob.objects.get(status="failed")
    assert mutate(staff, plan, "play").status_code == 200
    assert sync(agent_clients[0]).json()["commands"] == []
    assert Task.objects.count() == 1
    assert not ProjectPlan.objects.get(pk=plan.pk).playing
    services.resolve_worker_command(
        WorkerCommand.objects.get(pk=command["id"]), "error", error="Inspected; no execution"
    )
    services.update_status(None, job.attempt.task_id, "cancelled", human_actor="planner")
    mutate(staff, plan, "play")
    assert sync(agent_clients[0]).json()["commands"][0]["action"] == "create"


def test_stop_interrupts_and_does_not_replay_prompt(setup, agent_clients):
    staff, worker, plan = setup
    _, command = start(setup, agent_clients[0])
    accept(agent_clients[0], command)
    assert mutate(staff, plan, "stop").status_code == 200
    response = sync(agent_clients[0])
    assert [c["action"] for c in response.json()["commands"]] == ["interrupt"]
    assert sync(agent_clients[0]).json()["commands"] == []
    assert Task.objects.count() == 1


def test_board_edit_conflicts_auth_csrf_and_capability_scope(setup, agent_clients):
    staff, worker, plan = setup
    url = f"/api/projects/{plan.project.slug}/planning"
    assert Client().get(url).status_code == 401
    assert agent_clients[0].get(url).status_code == 403
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(User.objects.get(username="planner"))
    assert (
        csrf.post(
            url, {"operation": "play", "revision": 0}, content_type="application/json"
        ).status_code
        == 403
    )
    response = mutate(
        staff,
        plan,
        "card",
        card={
            "title": "New card",
            "description": "",
            "column": "Backlog",
            "labels": [],
            "checklist": [],
            "instructions": "",
        },
    )
    assert response.status_code == 200, response.content
    stale = staff.post(url, {"operation": "play", "revision": 0}, content_type="application/json")
    assert stale.status_code == 409
    _, command = start(setup, agent_clients[0], count=1)
    job = PlanningJob.objects.get(status="running")
    report_url = f"/api/planning/jobs/{job.pk}/report"
    bad = agent_clients[1].post(
        report_url,
        {"token": "x" * 43, "status": "done", "summary": "Fake"},
        content_type="application/json",
    )
    assert bad.status_code == 404
    heartbeat = agent_clients[1].post(
        report_url, {"token": token(job), "status": "heartbeat"}, content_type="application/json"
    )
    assert heartbeat.status_code == 200
    assert job.report_token_hash != token(job)
    assert "report_token_hash" not in staff.get(url).json()["jobs"][0]


def test_oversized_context_rolls_back_task_claim_and_create(setup, agent_clients):
    staff, worker, plan = setup
    plan.context = "x" * 25000
    plan.save()
    c = card(plan)
    assert mutate(staff, plan, "run", card_id=c.pk).status_code == 200
    response = sync(agent_clients[0])
    assert response.status_code == 200, response.content
    assert Task.objects.count() == WorkerCommand.objects.count() == 0
    plan.refresh_from_db()
    assert not plan.playing and "20,000" in plan.pause_reason


@pytest.mark.django_db(transaction=True)
def test_parallel_ticks_create_exactly_one_session(project):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL verifies concurrent orchestration row locks")
    agent = Agent.objects.create(name="runner")
    worker = services.provision_worker("worker-a", agent)
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    plan = ProjectPlan.objects.create(project=project, default_worker=worker, playing=True)
    c = card(plan)
    services.mutate_planning(
        project, "planner", {"operation": "enqueue", "revision": 0, "card_id": c.pk}
    )
    barrier = Barrier(2)

    def tick():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            with transaction.atomic():
                services.tick_planning_queues(worker)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(), range(2)))
    assert WorkerCommand.objects.filter(action="create").count() == 1
    assert PlanningJob.objects.filter(status="running").count() == 1
