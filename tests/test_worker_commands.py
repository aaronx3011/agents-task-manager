"""Durable remote commands tests (issue #12)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, WorkerCommand

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_worker(agent_clients):
    alpha = Agent.objects.get(name="alpha")
    worker = services.provision_worker("worker-a", alpha)
    worker.connected = True
    worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    return worker


@pytest.fixture
def staff_client(django_user_model):

    user = django_user_model.objects.create_user("root", password="pw", is_staff=True)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def session(online_worker):
    return services.store_sessions(
        online_worker, [{"id": "ses_1", "directory": "/repo", "title": "T"}]
    )[0]


def post_command(client, payload):
    return client.post("/api/commands", payload, content_type="application/json")


def post_sync(client, payload=None):
    envelope = {
        "worker": "worker-a",
        "protocol_version": 1,
        "meta": {},
        "sessions": [],
        "updates": [],
        "events": [],
        "results": [],
        "pending": {},
        "cursor": {},
    }
    if payload:
        envelope.update(payload)
    return client.post("/api/worker/sync", envelope, content_type="application/json")


def command_payload(**overrides):
    payload = {
        "worker": "worker-a",
        "action": "prompt",
        "sessionID": "ses_1",
        "payload": {"text": "hello"},
    }
    payload.update(overrides)
    return payload


class TestEnqueue:
    def test_agent_prompt_enqueue_returns_202(self, api_client, online_worker, session):
        response = post_command(api_client, command_payload())
        assert response.status_code == 202
        card = response.json()
        assert card["status"] == "queued" and card["action"] == "prompt"
        assert card["payload"]["text"] == "hello"
        assert card["createdBy"] == "alpha"
        assert WorkerCommand.objects.filter(worker=online_worker).count() == 1

    def test_create_command_without_session(self, api_client, online_worker, session):
        response = post_command(
            api_client,
            {
                "worker": "worker-a",
                "action": "create",
                "payload": {
                    "title": "New session",
                    "directory": "/repo",
                    "model": {"providerID": "p", "modelID": "m"},
                },
            },
        )
        assert response.status_code == 202
        card = response.json()
        assert card["sessionID"] is None
        assert card["payload"]["model"] == {"providerID": "p", "modelID": "m"}

    def test_create_rejects_session_id(self, api_client, online_worker, session):
        response = post_command(api_client, command_payload(action="create"))
        assert response.status_code == 400

    def test_prompt_requires_text(self, api_client, online_worker, session):
        response = post_command(api_client, command_payload(payload={}))
        assert response.status_code == 400

    def test_unknown_session_404(self, api_client, online_worker, session):
        response = post_command(api_client, command_payload(sessionID="ses_other"))
        assert response.status_code == 404

    def test_unknown_action_400(self, api_client, online_worker, session):
        response = post_command(api_client, command_payload(action="shell.exec"))
        assert response.status_code == 400

    def test_wrong_agent_worker_scope(self, agent_clients, online_worker, session):
        beta = agent_clients[1]
        response = post_command(beta, command_payload())
        assert response.status_code == 403

    def test_agent_never_arbitrary_cross_worker(self, agent_clients, online_worker, session):
        beta = agent_clients[1]
        response = post_command(beta, {"worker": "worker-a", "action": "create", "payload": {}})
        assert response.status_code == 403


class TestTargetChecks:
    @pytest.mark.parametrize(
        "catalog",
        [
            [{"providerID": "p", "modelID": "m", "name": "Connected model"}],
            [{"id": "p", "models": {"m": {"id": "m"}}}],
        ],
    )
    def test_model_selection_accepts_observed_catalog_shapes(
        self, api_client, online_worker, session, catalog
    ):
        online_worker.model_catalog = catalog
        online_worker.save(update_fields=["model_catalog"])
        response = post_command(
            api_client,
            command_payload(
                payload={"text": "hello", "model": {"providerID": "p", "modelID": "m"}}
            ),
        )
        assert response.status_code == 202
        response = post_command(
            api_client,
            command_payload(
                payload={"text": "hello", "model": {"providerID": "p", "modelID": "unknown"}}
            ),
        )
        assert response.status_code == 400

    def test_offline_worker_409(self, api_client, online_worker, session):
        online_worker.last_sync_at = None
        online_worker.save(update_fields=["last_sync_at"])
        response = post_command(api_client, command_payload())
        assert response.status_code == 409

    def test_disconnected_opencode_409(self, api_client, online_worker, session):
        online_worker.stream_connected = False
        online_worker.save(update_fields=["stream_connected"])
        response = post_command(api_client, command_payload())
        assert response.status_code == 409

    def test_disabled_worker_403(self, api_client, online_worker, session):
        services.set_worker_enabled(online_worker, False)
        response = post_command(api_client, command_payload())
        assert response.status_code == 403


class TestIdempotencyAndCaps:
    def test_absent_client_request_ids_do_not_collide(self, api_client, online_worker, session):
        first = post_command(api_client, command_payload())
        second = post_command(api_client, command_payload())
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] != second.json()["id"]
        assert list(WorkerCommand.objects.values_list("client_request_id", flat=True)) == [
            None,
            None,
        ]

    def test_repeated_client_request_id_returns_same(self, api_client, online_worker, session):
        first = post_command(api_client, command_payload(client_request_id="req-1"))
        assert first.status_code == 202
        second = post_command(api_client, command_payload(client_request_id="req-1"))
        assert second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert WorkerCommand.objects.count() == 1

    def test_same_id_different_payload_conflicts(self, api_client, online_worker, session):
        assert (
            post_command(api_client, command_payload(client_request_id="req-2")).status_code == 202
        )
        conflict = post_command(
            api_client, command_payload(payload={"text": "other"}, client_request_id="req-2")
        )
        assert conflict.status_code == 409

    def test_queue_cap_429(self, api_client, online_worker, session, settings):
        settings.WORKER_COMMAND_QUEUE_CAP = 3
        for index in range(3):
            assert (
                post_command(api_client, command_payload(client_request_id=f"r{index}")).status_code
                == 202
            )
        response = post_command(api_client, command_payload())
        assert response.status_code == 429

    def test_interaction_reply_requires_pending_request(self, api_client, online_worker, session):
        payload = {
            "worker": "worker-a",
            "action": "permission.reply",
            "sessionID": "ses_1",
            "payload": {"requestID": "per_missing", "reply": "once"},
        }
        response = post_command(api_client, payload)
        assert response.status_code == 404
        assert response.json()["detail"] == "Request is no longer pending on this session"


class TestDispatchAndResults:
    def test_dispatch_once_inside_sync(self, api_client, online_worker, session):
        first = post_command(api_client, command_payload())
        assert first.status_code == 202
        response = post_sync(api_client)
        body = response.json()
        assert len(body["commands"]) == 1
        command = body["commands"][0]
        assert command["id"] == first.json()["id"]
        assert command["status"] == "dispatched"
        # Second sync without results: nothing re-dispatched.
        second = post_sync(api_client)
        assert second.json()["commands"] == []

    def test_result_applies_done(self, api_client, online_worker, session):
        queued = post_command(api_client, command_payload())
        post_sync(api_client)
        payload = {
            "worker": "worker-a",
            "protocol_version": 1,
            "meta": {},
            "sessions": [],
            "updates": [],
            "events": [],
            "results": [
                {"commandID": queued.json()["id"], "status": "done", "result": {"id": "ses_2"}}
            ],
        }
        response = post_sync(api_client, payload)
        assert response.status_code == 200
        command = WorkerCommand.objects.get(pk=queued.json()["id"])
        assert command.status == "done"
        assert command.result == {"id": "ses_2"}
        assert command.completed_at is not None

    def test_result_error_and_uncertain(self, api_client, online_worker, session):
        queued = post_command(api_client, command_payload())
        post_sync(api_client)
        command_id = queued.json()["id"]
        uncertain = api_client.post(
            "/api/worker/sync",
            {
                "worker": "worker-a",
                "protocol_version": 1,
                "meta": {},
                "sessions": [],
                "updates": [],
                "events": [],
                "results": [
                    {
                        "commandID": command_id,
                        "status": "error",
                        "error": (
                            "Worker stopped during command delivery; outcome uncertain. "
                            "Not replayed; reconcile with the OpenCode session."
                        ),
                    }
                ],
            },
            content_type="application/json",
        )
        assert uncertain.status_code == 200
        command = WorkerCommand.objects.get(pk=command_id)
        assert command.delivery_uncertain and command.status == "dispatched"
        # Explicit staff reconciliation closes it.

    def test_foreign_result_ignored(self, api_client, agent_clients, online_worker, session):
        queued = post_command(api_client, command_payload())
        post_sync(api_client)
        payload = {
            "worker": "worker-a",
            "protocol_version": 1,
            "meta": {},
            "sessions": [],
            "updates": [],
            "events": [],
            "results": [{"commandID": "999999", "status": "done", "result": {}}],
        }
        response = post_sync(api_client, payload)
        assert response.status_code == 200
        command = WorkerCommand.objects.get(pk=queued.json()["id"])
        assert command.status == "dispatched"

    def test_repeated_same_result_idempotent(self, api_client, online_worker, session):
        queued = post_command(api_client, command_payload())
        post_sync(api_client)
        base = {
            "worker": "worker-a",
            "protocol_version": 1,
            "meta": {},
            "sessions": [],
            "updates": [],
            "events": [],
            "results": [
                {"commandID": queued.json()["id"], "status": "done", "result": {"id": "x"}}
            ],
        }
        assert post_sync(api_client, base).status_code == 200
        again = post_sync(api_client, base)
        assert again.status_code == 200
        assert WorkerCommand.objects.get(pk=queued.json()["id"]).status == "done"


class TestResolveAndReads:
    def test_staff_resolve_via_client(self, staff_client, api_client, online_worker, session):
        queued = post_command(api_client, command_payload())
        post_sync(api_client)
        response = staff_client.post(
            f"/api/commands/{queued.json()['id']}/resolve",
            {"status": "error", "error": "manually reconciled as failed"},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        assert response.json()["status"] == "error"

    def test_resolve_terminal_conflicts(self, staff_client, api_client, online_worker, session):
        queued = post_command(api_client, command_payload()).json()
        post_sync(api_client)
        assert (
            staff_client.post(
                f"/api/commands/{queued['id']}/resolve",
                {"status": "done", "result": {"id": "ses_x"}},
                content_type="application/json",
            ).status_code
            == 200
        )
        response = staff_client.post(
            f"/api/commands/{queued['id']}/resolve",
            {"status": "error", "error": "later"},
            content_type="application/json",
        )
        assert response.status_code == 409

    def test_list_and_detail_reads(self, staff_client, api_client, online_worker, session):
        card = post_command(api_client, command_payload()).json()
        response = staff_client.get("/api/commands", HTTP_ACCEPT="application/json")
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["items"]] == [card["id"]]
        detail = staff_client.get(f"/api/commands/{card['id']}")
        assert detail.json()["payload"]["text"] == "hello"
        # Agents may read only their own worker's commands.
        mine = api_client.get("/api/commands")
        assert {row["id"] for row in mine.json()["items"]} == {card["id"]}

    def test_agents_read_scoped_to_own_worker(self, agent_clients, online_worker, session):
        beta = agent_clients[1]
        beta_worker = services.provision_worker("worker-b", Agent.objects.get(name="beta"))
        beta_worker.connected = True
        beta_worker.stream_connected = True
        beta_worker.last_sync_at = timezone.now()
        beta_worker.save()
        post_command(agent_clients[0], command_payload())
        response = beta.get("/api/commands?worker=worker-a")
        assert response.status_code == 200
        assert response.json()["items"] == []


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Concurrent dispatch requires PostgreSQL"
)
@pytest.mark.django_db(transaction=True)
class TestPostgresConcurrentDispatch:
    def test_two_syncs_deliver_each_once(self, api_client, online_worker, session):
        command = post_command(api_client, command_payload()).json()
        barrier = Barrier(2)

        def run(idx):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = post_sync(Client(**api_client.defaults))
                assert response.status_code == 200
                return response.json()["commands"]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(run, range(2)))
        assert [card["id"] for batch in batches for card in batch] == [command["id"]]
        cmd = WorkerCommand.objects.get(pk=command["id"])
        assert cmd.status == "dispatched"
        assert cmd.dispatched_at is not None
