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


def test_pipe_reads_are_agent_or_viewer_only_and_project_scoped(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    stage = pipe.stages.order_by("position").first()
    stage.artifact = "PLAN ARTIFACT"
    stage.save(update_fields=["artifact"])

    url = f"/api/projects/{plan.project.slug}/pipes"
    anonymous = Client()
    assert anonymous.get(url).status_code in {401, 403}
    assert anonymous.get(f"{url}/{pipe.pk}").status_code in {401, 403}
    assert anonymous.post(url).status_code in {401, 403}

    agent = clients["worker-plan"]
    listed = agent.get(url)
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    assert body["pipes"][0]["stages"][0]["artifact"] == "PLAN ARTIFACT"
    single = agent.get(f"{url}/{pipe.pk}")
    assert single.status_code == 200
    assert single.json()["pipe"]["stages"][0]["worker"] == "worker-plan"

    viewer = Client()
    viewer.force_login(User.objects.create_user("pipe-viewer"))
    assert viewer.get(url).status_code == 200
    assert viewer.get(f"{url}/{pipe.pk}").status_code == 200
    assert viewer.post(url).status_code in {403, 405}

    assert agent.get(f"{url}/{pipe.pk + 1}").status_code == 404
    assert agent.get("/api/projects/unknown/pipes").status_code == 404


def test_pipe_reads_validate_pagination(setup):
    staff, workers, plan, clients = setup
    create_pipe(staff, plan, stage_payload(workers))
    url = f"/api/projects/{plan.project.slug}/pipes"
    agent = clients["worker-plan"]
    assert agent.get(f"{url}?limit=0").status_code == 400
    assert agent.get(f"{url}?limit=101").status_code == 400
    assert agent.get(f"{url}?offset=-1").status_code == 400
    assert agent.get(f"{url}?limit=abc").status_code == 400
    page = agent.get(f"{url}?limit=1&offset=5").json()
    assert page["count"] == 1 and page["pipes"] == []


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


def test_voice_create_pipe_queues_without_playing(setup):
    staff, workers, plan, clients = setup
    payload = stage_payload(workers)
    payload["play"] = False

    result = services.voice_create_pipe(plan.project, "router", payload)

    pipe = Pipe.objects.get(pk=result["pipe"])
    job = PlanningJob.objects.get(pipe_stage__pipe=pipe)
    plan.refresh_from_db()
    assert result["first_job"] == job.pk
    assert result["jobs_ahead"] == 0
    assert job.status == "queued"
    assert pipe.stages.get(position=1).status == "queued"
    assert not plan.playing
    assert result["state"].startswith("Pipe added to the queue but it is not playing")


def test_voice_stop_pipe_cancels_a_queued_stage(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))

    result = services.voice_stop_pipe(plan.project, "router", pipe.pk)

    pipe.refresh_from_db()
    plan.refresh_from_db()
    job = PlanningJob.objects.get(pipe_stage__pipe=pipe)
    assert result["interrupt"] is None
    assert pipe.status == Pipe.Status.FAILED
    assert pipe.stages.get(position=1).status == PipeStage.Status.SKIPPED
    assert job.status == "cancelled"
    assert job.detail == "Pipe stopped by router"
    assert not plan.playing


def test_voice_stop_pipe_interrupts_a_running_stage(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    launch_stage(clients, "worker-plan", "ses_stop")
    job = PlanningJob.objects.get(pipe_stage__pipe=pipe, pipe_stage__position=1)
    assert job.status == "running"
    assert pipe.stages.get(position=1).status == PipeStage.Status.RUNNING

    result = services.voice_stop_pipe(plan.project, "router", pipe.pk)

    pipe.refresh_from_db()
    job.refresh_from_db()
    assert result["interrupt"]["status"] == "queued"
    assert pipe.status == Pipe.Status.FAILED
    assert pipe.stages.get(position=1).status == PipeStage.Status.FAILED
    assert job.status == "failed"
    assert job.stop_command_id is not None


def test_planning_api_pipe_stop_cancels_a_queued_stage(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))

    response = mutate(staff, plan, "pipe_stop", pipe_id=pipe.pk)

    assert response.status_code == 200, response.content
    pipe.refresh_from_db()
    job = PlanningJob.objects.get(pipe_stage__pipe=pipe)
    assert pipe.status == Pipe.Status.FAILED
    assert job.status == "cancelled"
    assert pipe.stages.get(position=1).status == PipeStage.Status.SKIPPED


def test_voice_create_pipe_play_starts_the_queue(setup):
    staff, workers, plan, clients = setup

    result = services.voice_create_pipe(plan.project, "router", stage_payload(workers))

    plan.refresh_from_db()
    assert plan.playing
    assert result["queue_playing"] is True
    assert result["state"].startswith("Pipe accepted and queued")


def stage_map(pipe):
    return {stage.position: stage for stage in pipe.stages.order_by("position")}


def test_edit_queued_stage_retargets_job_and_reorders_tail(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    stages = stage_map(pipe)
    queued = PlanningJob.objects.get(pipe_stage__pipe=pipe, status="queued")
    assert queued.pipe_stage_id == stages[1].pk

    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={
            "pipe_id": pipe.pk,
            "title": "Ship feature v2",
            "stages": [
                {
                    "id": stages[1].pk,
                    "name": "Plan v2",
                    "role": "plan",
                    "worker": "worker-exec",
                    "model": EXEC_MODEL,
                    "directory": "/workspace/custom",
                    "instructions": "REWRITTEN PLAN INSTRUCTIONS",
                },
                {"id": stages[3].pk},
                {
                    "name": "Review",
                    "role": "custom",
                    "worker": "worker-test",
                    "model": TEST_MODEL,
                },
            ],
        },
    )
    assert response.status_code == 200, response.content
    pipe.refresh_from_db()
    queued.refresh_from_db()
    stages[1].refresh_from_db()
    assert pipe.title == "Ship feature v2"
    assert pipe.on_failure == "stop"
    assert queued.worker.name == "worker-exec"
    assert queued.snapshot["model"] == EXEC_MODEL
    assert queued.snapshot["directory"] == "/workspace/custom"
    assert "REWRITTEN PLAN INSTRUCTIONS" in queued.snapshot["instructions"]
    assert stages[1].worker.name == "worker-exec"
    assert stages[1].card.worker.name == "worker-exec"
    assert stages[1].card.model == EXEC_MODEL
    assert not PipeStage.objects.filter(pk=stages[2].pk).exists()
    assert PlanningCard.objects.get(pk=stages[2].card_id).archived
    assert [(s.position, s.name) for s in pipe.stages.order_by("position")] == [
        (1, "Plan v2"),
        (2, "Test"),
        (3, "Review"),
    ]

    launch_stage(clients, "worker-exec", "ses_edit")
    queued.refresh_from_db()
    assert "REWRITTEN PLAN INSTRUCTIONS" in queued.prompt
    report_stage(clients, "worker-exec", queued, "done", "EDITED PLAN ARTIFACT")
    sync_worker(clients["worker-exec"], "worker-exec")
    next_job = PlanningJob.objects.get(status="queued", pipe_stage__pipe=pipe)
    assert next_job.worker.name == "worker-test"
    assert next_job.pipe_stage.name == "Test"


def test_edit_refuses_running_stages_and_finished_pipes(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    stages = stage_map(pipe)
    launch_stage(clients, "worker-plan", "ses_locked")
    running = PlanningJob.objects.get(pipe_stage__pipe=pipe, status="running")

    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={
            "pipe_id": pipe.pk,
            "stages": [{"id": stages[1].pk, "name": "Renamed while running"}],
        },
    )
    assert response.status_code == 409

    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={"pipe_id": pipe.pk, "stages": [{"id": stages[2].pk}]},
    )
    assert response.status_code == 409

    response = mutate(staff, plan, "pipe_remove", pipe_id=pipe.pk)
    assert response.status_code == 409
    assert b"running stage" in response.content

    assert mutate(staff, plan, "pipe_stop", pipe_id=pipe.pk).status_code == 200
    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={"pipe_id": pipe.pk, "title": "Too late"},
    )
    assert response.status_code == 409

    response = mutate(staff, plan, "pipe_remove", pipe_id=pipe.pk)
    assert response.status_code == 200, response.content
    pipe.refresh_from_db()
    assert pipe.archived and pipe.status == Pipe.Status.FAILED
    board = staff.get(f"/api/projects/{plan.project.slug}/planning").json()
    assert all(item["id"] != pipe.pk for item in board["pipes"])
    assert running.pk in set(
        PlanningJob.objects.filter(pipe_stage__pipe=pipe).values_list("pk", flat=True)
    )


def test_edit_validates_loop_policy_without_partial_change(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers, failure="stop"))
    stages = stage_map(pipe)
    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={
            "pipe_id": pipe.pk,
            "on_failure": "loop",
            "stages": [{"id": stages[1].pk}, {"id": stages[3].pk}],
        },
    )
    assert response.status_code == 400
    pipe.refresh_from_db()
    assert pipe.on_failure == "stop"
    assert pipe.stages.count() == 3

    response = mutate(
        staff,
        plan,
        "pipe_edit",
        pipe_edit={"pipe_id": pipe.pk, "on_failure": "loop", "max_retries": 0},
    )
    assert response.status_code == 400
    pipe.refresh_from_db()
    assert pipe.on_failure == "stop" and pipe.max_retries == 1


def test_remove_waiting_pipe_cancels_jobs_and_archives(setup):
    staff, workers, plan, clients = setup
    pipe = create_pipe(staff, plan, stage_payload(workers))
    job = PlanningJob.objects.get(pipe_stage__pipe=pipe, status="queued")
    response = mutate(staff, plan, "pipe_remove", pipe_id=pipe.pk)
    assert response.status_code == 200, response.content
    pipe.refresh_from_db()
    job.refresh_from_db()
    assert pipe.archived and pipe.status == Pipe.Status.CANCELLED
    assert job.status == "cancelled"
    assert set(pipe.stages.values_list("status", flat=True)) == {"skipped"}
    board = staff.get(f"/api/projects/{plan.project.slug}/planning").json()
    assert all(item["id"] != pipe.pk for item in board["pipes"])
    assert any(item["pipe"] == pipe.pk for item in board["jobs"])  # history kept
    summary = services.voice_queue_read(plan.project)
    assert all(item["pipe"] != pipe.pk for item in summary["pipes"])
