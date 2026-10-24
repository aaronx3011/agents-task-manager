"""Task execution attempts tests (issue #13)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import (
    Agent,
    Task,
    TaskEvent,
    TaskExecutionAttempt,
    WorkerCommand,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(db):
    from coordinator.models import Project

    return Project.objects.create(slug="coordinator", name="Coordinator")


@pytest.fixture
def agents(agent_clients):
    return [Agent.objects.get(name=name) for name in ("alpha", "beta")]


@pytest.fixture
def staff_client():
    user = User.objects.create_user("root", password="pw", is_staff=True)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def task(project, agents):
    return services.create_task(
        agent=agents[0],
        project=project,
        title="Remote execution task",
        description="Whatever",
    )


@pytest.fixture
def claimed(task, agents):
    services.claim_task(agents[0], task.pk)
    task.refresh_from_db()
    return task


def online_worker(agent, name="worker-a"):
    worker = services.provision_worker(name, agent)
    worker.connected = True
    worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    return worker


@pytest.fixture
def worker(claimed):
    return online_worker(claimed.claimed_by)


SYNC_BASE = {
    "protocol_version": 1,
    "meta": {},
    "sessions": [],
    "updates": [],
    "events": [],
    "results": [],
    "pending": {},
    "cursor": {},
}


def post_sync(client, payload=None):
    envelope = {"worker": "worker-a", **SYNC_BASE}
    if payload:
        envelope.update(payload)
    return client.post("/api/worker/sync", envelope, content_type="application/json")


def seeded_sessions(worker):
    return services.store_sessions(
        worker,
        [
            {"id": "ses_new", "directory": "/new", "title": "Created"},
            {"id": "ses_other", "directory": "/other", "title": "Unrelated"},
        ],
    )


class TestAuthorization:
    def test_requires_claim_first(self, project, agents, staff_client):
        task = services.create_task(agent=agents[0], project=project, title="t", description="")
        worker = online_worker(agents[0])
        services.claim_task(agents[1], task.pk)  # beta claims first
        response = staff_client.post(
            f"/api/tasks/{task.pk}/launch", {"worker": worker.name}, content_type="application/json"
        )
        assert response.status_code != 201  # staff launch needs the CLAIM OWNER

    def test_staff_launch_rejects_unclaimed(self, staff_client, project, agents):
        task = services.create_task(agent=agents[0], project=project, title="t", description="")
        worker = online_worker(agents[0])
        response = staff_client.post(
            f"/api/tasks/{task.pk}/launch", {"worker": worker.name}, content_type="application/json"
        )
        assert response.status_code == 409


class TestLaunchWorkflow:
    def test_owner_launch_creates_attempt_and_command(self, api_client, claimed, worker):
        response = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["attempt"]["outcome"] == "pending"
        assert body["attempt"]["worker"] == "worker-a"
        assert body["createCommand"]["action"] == "create"
        assert TaskExecutionAttempt.objects.count() == 1
        assert claimed.events.filter(event_type=TaskEvent.Type.EXECUTION_LAUNCHED).exists()

    def test_second_active_attempt_conflicts(self, api_client, claimed, worker):
        assert (
            api_client.post(
                f"/api/tasks/{claimed.pk}/launch",
                {"worker": "worker-a"},
                content_type="application/json",
            ).status_code
            == 201
        )
        again = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        assert again.status_code == 409
        body = again.json()
        assert "attempt" in body or "already active" in body.get("detail", "")
        assert TaskExecutionAttempt.objects.count() == 1

    def test_recommendation_no_hidden_duplicates_on_retry(self, api_client, claimed, worker):
        # Deterministic reproducible id (bridge sends idempotency keys on retries).
        first = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a", "client_request_id": "launch-1"},
            content_type="application/json",
        )
        assert first.status_code == 201
        first.json()
        retry = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a", "client_request_id": "launch-1"},
            content_type="application/json",
        )
        assert retry.status_code == 409
        assert "createCommand" in retry.json()
        assert WorkerCommand.objects.filter(status="queued").count() == 1
        assert TaskExecutionAttempt.objects.count() == 1

    def test_directory_defaults_to_worker_create_directory(self, api_client, claimed, worker):
        worker.create_directory = "/workspace/default"
        worker.save(update_fields=["create_directory", "updated_at"])
        api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        attempt = TaskExecutionAttempt.objects.get(task=claimed)
        assert attempt.directory == "/workspace/default"
        command = WorkerCommand.objects.get(pk=attempt.create_command_id)
        assert command.payload["directory"] == "/workspace/default"

    def test_session_sync_does_not_renew_task_ownership(self, api_client, claimed, worker):
        api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        before = claimed.heartbeat_at
        post_sync(api_client, {"meta": {}})
        claimed.refresh_from_db()
        assert claimed.heartbeat_at == before  # worker heartbeat touch ≠ task renew
        assert claimed.status == Task.Status.IN_PROGRESS  # task lifecycle untouched

    def test_prompt_acceptance_not_task_completion(self, api_client, claimed, worker):
        api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        post_sync(
            api_client,
            {
                "sessions": [{"id": "ses_new", "directory": "/new", "title": "Created"}],
                "updates": [],
                "events": [],
            },
        )
        post_sync(
            api_client,
            {
                "results": [],
                "events": [
                    {
                        "type": "session.status",
                        "sessionID": "ses_new",
                        "payload": {"properties": {"status": {"type": "busy"}}},
                    }
                ],
            },
        )
        claimed.refresh_from_db()
        assert claimed.status == Task.Status.IN_PROGRESS


class TestAttemptCorrelation:
    def _launch(self, api_client, claimed, worker):
        response = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a", "client_request_id": "launch-err"},
            content_type="application/json",
        )
        body = response.json()
        attempt_id = body["attempt"]["id"]
        command_id = body["createCommand"]["id"]
        post_sync(api_client)  # dispatch
        return attempt_id, command_id

    def test_create_error_marks_attempt_error(self, api_client, claimed, worker):
        attempt_id, command_id = self._launch(api_client, claimed, worker)
        post_sync(
            api_client,
            {
                "results": [
                    {"commandID": command_id, "status": "error", "error": "OpenCode create failed"}
                ]
            },
        )
        attempt = TaskExecutionAttempt.objects.get(pk=attempt_id)
        assert attempt.outcome == "error"
        assert "failed" in attempt.outcome_detail
        assert attempt.resolved_at is not None

    def test_lost_create_ack_is_uncertain(self, api_client, claimed, worker):
        attempt_id, command_id = self._launch(api_client, claimed, worker)
        post_sync(
            api_client,
            {
                "results": [
                    {
                        "commandID": command_id,
                        "status": "uncertain",
                        "error": (
                            "Delivery uncertain after restart; reconcile with the OpenCode session."
                        ),
                    }
                ]
            },
        )
        attempt = TaskExecutionAttempt.objects.get(pk=attempt_id)
        assert attempt.outcome == "uncertain"
        assert attempt.remote_session_id in (None, "")
        # No replacement attempt is created silently.
        assert TaskExecutionAttempt.objects.count() == 1

    def test_create_done_correlates_remote_session(self, api_client, claimed, worker):
        attempt_id, command_id = self._launch(api_client, claimed, worker)
        post_sync(
            api_client,
            {
                "results": [
                    {"commandID": command_id, "status": "done", "result": {"id": "ses_created"}}
                ]
            },
        )
        attempt = TaskExecutionAttempt.objects.get(pk=attempt_id)
        assert attempt.outcome == "accepted"
        assert attempt.remote_session_id == "ses_created"
        session = services.store_sessions(worker, [{"id": "ses_created", "directory": "/d"}])[0]
        attempt.refresh_from_db()
        assert attempt.session_id == session.pk  # observed session roped into the attempt


class TestExplicitLinks:
    def test_foreign_worker_session_cannot_link(
        self, staff_client, api_client, claimed, worker, agents
    ):
        beta = online_worker(agents[1], name="worker-b")
        services.store_sessions(beta, [{"id": "ses_foreign", "directory": "/b"}])
        response = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        attempt_id = response.json()["attempt"]["id"]
        ok = staff_client.post(
            f"/api/attempts/{attempt_id}/link",
            {"sessionID": "ses_foreign"},
            content_type="application/json",
        )
        assert ok.status_code == 404  # foreign worker session is not visible to this attempt

    def test_link_after_observation(self, staff_client, api_client, claimed, worker):
        response = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        attempt_id = response.json()["attempt"]["id"]
        post_sync(
            api_client, {"sessions": [{"id": "ses_new", "directory": "/d", "title": "Created"}]}
        )
        linked = staff_client.post(
            f"/api/attempts/{attempt_id}/link",
            {"sessionID": "ses_new"},
            content_type="application/json",
        )
        assert linked.status_code == 200
        assert linked.json()["remoteSessionId"] == "ses_new"
        # Attempt detail the task detail.
        detail = staff_client.get(f"/api/tasks/{claimed.pk}")
        attempts = detail.json()["attempts"]
        assert attempts and attempts[0]["id"] == attempt_id

    def test_claim_expiry_does_not_silently_relaunch(self, api_client, claimed, worker):
        response = api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        attempt_id = response.json()["attempt"]["id"]
        services.release_task(claimed.claimed_by, claimed.pk, reason="handoff")
        # The running attempt survives claim release and must be resolved explicitly;
        # nothing auto-launches a replacement execution.
        assert TaskExecutionAttempt.objects.get(pk=attempt_id).outcome == "pending"
        new_owner = Agent.objects.get(name="beta")
        services.claim_task(new_owner, claimed.pk)
        with pytest.raises(services.TaskError):
            services.launch_task_execution(new_owner, "beta", claimed.pk, worker)
        assert TaskExecutionAttempt.objects.count() == 1
        # After the earlier attempt leaves pending (accepted create), a replacement
        # attempt may be launched explicitly by the new claim owner.
        attempt = TaskExecutionAttempt.objects.get(pk=attempt_id)
        post_sync(
            api_client,
            {
                "results": [
                    {
                        "commandID": str(attempt.create_command_id),
                        "status": "done",
                        "result": {"id": "ses_x"},
                    }
                ]
            },
        )
        attempt.refresh_from_db()
        assert attempt.outcome == "accepted"
        # Replacement launch needs a worker bound to the new owner.
        beta_worker = online_worker(new_owner, name="worker-b")
        services.launch_task_execution(new_owner, "beta", claimed.pk, beta_worker)
        assert TaskExecutionAttempt.objects.count() == 2
        assert TaskExecutionAttempt.objects.filter(task=claimed, outcome="pending").count() == 1


class TestAttemptOutcomesIndependence:
    def test_status_change_rules_preserved(self, api_client, claimed, worker):
        api_client.post(
            f"/api/tasks/{claimed.pk}/launch",
            {"worker": "worker-a"},
            content_type="application/json",
        )
        task = services.update_status(
            claimed.claimed_by, claimed.pk, "review", reason="ready", result_summary="executed"
        )
        assert task.status == Task.Status.REVIEW


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Concurrent launch requires PostgreSQL"
)
@pytest.mark.django_db(transaction=True)
class TestConcurrentLaunch:
    def test_double_launch_stays_single_attempt(self, api_client, claimed, worker):
        barrier = Barrier(2)

        def run(idx):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = Client(**api_client.defaults).post(
                    f"/api/tasks/{claimed.pk}/launch",
                    {"worker": worker.name, "client_request_id": f"conc-{idx}"},
                    content_type="application/json",
                )
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(run, range(2)))
        assert sorted(statuses) == [201, 409]
        # Exactly one pending attempt exists for the task.
        assert TaskExecutionAttempt.objects.filter(outcome="pending").count() == 1
