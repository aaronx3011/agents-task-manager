"""Multi-stage pipes: ordered stages, handoff, retries and checkpoints (#437)."""

import re

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import (
    Agent,
    Pipe,
    PipeStage,
    PlanningCard,
    PlanningJob,
    ProjectPlan,
)

pytestmark = pytest.mark.django_db

PLAN_MODEL = {"providerID": "test", "modelID": "plan-model"}
EXEC_MODEL = {"providerID": "test", "modelID": "exec-model"}
TEST_MODEL = {"providerID": "test", "modelID": "test-model"}


@pytest.fixture
def setup(project):
    staff = Client()
    staff.force_login(User.objects.create_user("planner", is_staff=True))
    workers = {}
    clients = {}
    for name, model in [
        ("worker-plan", PLAN_MODEL),
        ("worker-exec", EXEC_MODEL),
        ("worker-test", TEST_MODEL),
    ]:
        agent = Agent(name=f"agent-{name}")
        key = agent.set_new_api_key()
        agent.save()
        worker = services.provision_worker(name, agent)
        worker.connected = worker.stream_connected = True
        worker.last_sync_at = timezone.now()
        worker.default_model = model
        worker.model_catalog = [model]
        worker.agent_catalog = [{"name": "build", "mode": "primary"}]
        worker.directory = f"/workspace/{name}"
        worker.save()
        workers[name] = worker
        clients[name] = Client(HTTP_AUTHORIZATION=f"Bearer {key}")
    plan = ProjectPlan.objects.create(
        project=project,
        default_worker=workers["worker-plan"],
        directory="/workspace",
        context="Project architecture",
        instructions="Run relevant checks",
    )
    return staff, workers, plan, clients


def mutate(staff, plan, operation, **values):
    plan.refresh_from_db()
    return staff.post(
        f"/api/projects/{plan.project.slug}/planning",
        {"revision": plan.revision, "operation": operation, **values},
        content_type="application/json",
    )


def sync_worker(client, worker, sessions=None, results=None):
    return client.post(
        "/api/worker/sync",
        {
            "worker": worker,
            "protocol_version": 1,
            "meta": {"connected": True, "streamConnected": True},
            "sessions": sessions or [],
            "updates": [],
            "events": [],
            "results": results or [],
            "pending": {"complete": True, "permissions": [], "questions": []},
            "cursor": {},
        },
        content_type="application/json",
    )


def token(job):
    return re.search(r"token=([A-Za-z0-9_-]+)", job.prompt).group(1)


def stage_payload(workers, approval_after=0, failure="stop", retries=1):
    stages = [
        {
            "name": "Plan",
            "role": "plan",
            "instructions": "Write the implementation plan",
            "worker": workers["worker-plan"].name,
            "model": PLAN_MODEL,
            "requires_approval": approval_after == 1,
        },
        {
            "name": "Execute",
            "role": "execute",
            "instructions": "Implement the plan",
            "worker": workers["worker-exec"].name,
            "model": EXEC_MODEL,
        },
        {
            "name": "Test",
            "role": "test",
            "instructions": "Verify the implementation",
            "worker": workers["worker-test"].name,
            "model": TEST_MODEL,
        },
    ]
    return {
        "title": "Ship feature",
        "description": "Plan, build and verify",
        "on_failure": failure,
        "max_retries": retries,
        "play": True,
        "stages": stages,
    }


def create_pipe(staff, plan, data):
    response = mutate(staff, plan, "pipe_create", pipe=data)
    assert response.status_code == 200, response.content
    return Pipe.objects.latest("pk")


def launch_stage(clients, worker_name, sid):
    response = sync_worker(
        clients[worker_name], worker_name, sessions=[{"id": sid, "directory": "/workspace"}]
    )
    assert response.status_code == 200, response.content
    commands = response.json()["commands"]
    assert len(commands) == 1 and commands[0]["action"] == "create", commands
    create = commands[0]
    response = sync_worker(
        clients[worker_name],
        worker_name,
        sessions=[{"id": sid, "directory": "/workspace"}],
        results=[{"commandID": create["id"], "status": "done", "result": {"id": sid}}],
    )
    assert response.status_code == 200, response.content
    commands = response.json()["commands"]
    assert len(commands) == 1 and commands[0]["action"] == "prompt", commands
    prompt = commands[0]
    response = sync_worker(
        clients[worker_name],
        worker_name,
        results=[{"commandID": prompt["id"], "status": "done", "result": {"accepted": True}}],
    )
    assert response.status_code == 200, response.content
    return prompt


def report_stage(clients, worker_name, job, status, summary):
    job.refresh_from_db()
    response = clients[worker_name].post(
        f"/api/planning/jobs/{job.pk}/report",
        {"token": token(job), "status": status, "summary": summary},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def test_three_stage_pipe_dispatches_workers_models_and_hands_off(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    assert pipe.stages.count() == 3
    assert PipeStage.objects.filter(pipe=pipe, card__isnull=False).count() == 3
    first = PlanningJob.objects.get(status="queued")
    assert first.worker.name == "worker-plan"
    assert first.snapshot["model"] == PLAN_MODEL
    assert first.pipe_stage.position == 1

    launch_stage(clients, "worker-plan", "ses_plan")
    plan_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=1)
    assert plan_job.snapshot["directory"] == "/workspace"
    assert "## Pipe: Ship feature — stage 1/3 'Plan'" in plan_job.prompt
    assert "None (this is the first stage)" in plan_job.prompt
    report_stage(clients, "worker-plan", plan_job, "done", "PLAN ARTIFACT: add /login")
    sync_worker(clients["worker-plan"], "worker-plan")
    plan_job.refresh_from_db()
    assert plan_job.status == "completed"
    assert pipe.stages.get(position=1).artifact == "PLAN ARTIFACT: add /login"
    execute_job = PlanningJob.objects.get(status="queued")
    assert execute_job.worker.name == "worker-exec"
    assert execute_job.pipe_stage.position == 2

    launch_stage(clients, "worker-exec", "ses_exec")
    report_stage(
        clients, "worker-exec", execute_job, "done", "IMPLEMENTATION ARTIFACT: login endpoint"
    )
    sync_worker(clients["worker-exec"], "worker-exec")
    test_job = PlanningJob.objects.get(status="queued")
    assert test_job.worker.name == "worker-test"
    assert test_job.pipe_stage.position == 3

    launch_stage(clients, "worker-test", "ses_test")
    test_job.refresh_from_db()
    assert "PLAN ARTIFACT: add /login" in test_job.prompt
    assert "IMPLEMENTATION ARTIFACT: login endpoint" in test_job.prompt
    report_stage(clients, "worker-test", test_job, "done", "TEST ARTIFACT: 19 passed")
    response = sync_worker(clients["worker-test"], "worker-test")
    assert response.status_code == 200
    pipe.refresh_from_db()
    assert pipe.status == Pipe.Status.COMPLETED
    assert [stage.status for stage in pipe.stages.order_by("position")] == [
        "completed",
        "completed",
        "completed",
    ]
    board = staff.get(f"/api/projects/{plan.project.slug}/planning").json()
    rows = [job for job in board["jobs"] if job["pipe"] == pipe.pk]
    assert {row["stage"]["name"] for row in rows} == {"Plan", "Execute", "Test"}
    assert {row["worker"] for row in rows} == {"worker-plan", "worker-exec", "worker-test"}
    assert board["pipes"][0]["stages"][2]["artifact"] == "TEST ARTIFACT: 19 passed"


def test_checkpoint_pauses_and_play_is_refused_until_approval(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers, approval_after=1))
    launch_stage(clients, "worker-plan", "ses_plan")
    plan_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=1)
    report_stage(clients, "worker-plan", plan_job, "done", "PLAN ARTIFACT")
    sync_worker(clients["worker-plan"], "worker-plan")
    pipe.refresh_from_db()
    plan.refresh_from_db()
    assert pipe.status == Pipe.Status.AWAITING_APPROVAL
    assert not plan.playing
    assert not PlanningJob.objects.filter(status="queued").exists()

    response = mutate(staff, plan, "play")
    assert response.status_code == 409
    assert b"awaits checkpoint approval" in response.content

    response = mutate(staff, plan, "pipe_approve", pipe_id=pipe.pk)
    assert response.status_code == 200, response.content
    pipe.refresh_from_db()
    plan.refresh_from_db()
    assert pipe.approved_stage == 1 and pipe.status == Pipe.Status.RUNNING
    assert plan.playing
    queued = PlanningJob.objects.get(status="queued")
    assert queued.pipe_stage.position == 2


def test_loop_policy_retries_execute_and_test_then_completes(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers, failure="loop", retries=1))
    launch_stage(clients, "worker-plan", "ses_plan")
    plan_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=1)
    report_stage(clients, "worker-plan", plan_job, "done", "PLAN ARTIFACT")
    sync_worker(clients["worker-plan"], "worker-plan")
    launch_stage(clients, "worker-exec", "ses_exec")
    execute_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=2)
    report_stage(clients, "worker-exec", execute_job, "done", "FIRST IMPLEMENTATION")
    sync_worker(clients["worker-exec"], "worker-exec")
    launch_stage(clients, "worker-test", "ses_test")
    test_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=3)
    report_stage(clients, "worker-test", test_job, "blocked", "TEST FAILED: login broken")
    sync_worker(clients["worker-test"], "worker-test")

    pipe.refresh_from_db()
    assert pipe.retries_used == 1
    assert pipe.status in {Pipe.Status.QUEUED, Pipe.Status.RUNNING}
    test_job.refresh_from_db()
    assert test_job.status == "cancelled"
    assert "TEST FAILED: login broken" in pipe.last_failure
    retry_execute = PlanningJob.objects.get(status="queued")
    assert retry_execute.pipe_stage.position == 2
    assert retry_execute.card_id != execute_job.card_id
    launch_stage(clients, "worker-exec", "ses_exec2")
    retry_execute.refresh_from_db()
    assert "TEST FAILED: login broken" in retry_execute.prompt
    report_stage(clients, "worker-exec", retry_execute, "done", "SECOND IMPLEMENTATION")
    sync_worker(clients["worker-exec"], "worker-exec")
    launch_stage(clients, "worker-test", "ses_test2")
    retry_test = (
        PlanningJob.objects.filter(pipe_stage__pipe=pipe, pipe_stage__position=3)
        .order_by("-pk")
        .first()
    )
    retry_test.refresh_from_db()
    assert "SECOND IMPLEMENTATION" in retry_test.prompt
    report_stage(clients, "worker-test", retry_test, "done", "TEST PASSED")
    sync_worker(clients["worker-test"], "worker-test")
    pipe.refresh_from_db()
    assert pipe.status == Pipe.Status.COMPLETED
    assert pipe.retries_used == 1


def test_stop_policy_fails_and_pauses_for_a_human(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers, failure="stop"))
    launch_stage(clients, "worker-plan", "ses_plan")
    plan_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=1)
    report_stage(clients, "worker-plan", plan_job, "done", "PLAN ARTIFACT")
    sync_worker(clients["worker-plan"], "worker-plan")
    launch_stage(clients, "worker-exec", "ses_exec")
    execute_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=2)
    report_stage(clients, "worker-exec", execute_job, "done", "IMPLEMENTATION")
    sync_worker(clients["worker-exec"], "worker-exec")
    launch_stage(clients, "worker-test", "ses_test")
    test_job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=3)
    report_stage(clients, "worker-test", test_job, "blocked", "TEST FAILED")
    sync_worker(clients["worker-test"], "worker-test")
    pipe.refresh_from_db()
    plan.refresh_from_db()
    assert pipe.status == Pipe.Status.FAILED
    assert not plan.playing
    assert "failed" in plan.pause_reason.lower()
    test_job.refresh_from_db()
    assert test_job.status == "failed"
    assert not PlanningJob.objects.filter(status="queued").exists()


def test_refusals_leave_no_partial_pipe(setup):
    staff, workers, plan, clients = setup
    before = (Pipe.objects.count(), PlanningCard.objects.count(), PlanningJob.objects.count())

    offline = workers["worker-test"]
    offline.connected = offline.stream_connected = False
    offline.save()
    bad = stage_payload(workers)
    response = mutate(staff, plan, "pipe_create", pipe=bad)
    assert response.status_code == 409
    assert b"offline" in response.content
    offline.connected = offline.stream_connected = True
    offline.save()

    bad = stage_payload(workers)
    bad["stages"][1]["model"] = {"providerID": "test", "modelID": "missing"}
    response = mutate(staff, plan, "pipe_create", pipe=bad)
    assert response.status_code == 400
    assert b"model catalog" in response.content

    bad = stage_payload(workers, failure="loop")
    bad["stages"] = [stage for stage in bad["stages"] if stage["role"] != "test"]
    response = mutate(staff, plan, "pipe_create", pipe=bad)
    assert response.status_code == 400

    assert (
        Pipe.objects.count(),
        PlanningCard.objects.count(),
        PlanningJob.objects.count(),
    ) == before

    viewer = Client()
    viewer.force_login(User.objects.create_user("viewer"))
    plan.refresh_from_db()
    response = viewer.post(
        f"/api/projects/{plan.project.slug}/planning",
        {
            "revision": plan.revision,
            "operation": "pipe_create",
            "pipe": stage_payload(workers),
        },
        content_type="application/json",
    )
    assert response.status_code in {401, 403}
