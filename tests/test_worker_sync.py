"""POST /api/worker/sync acceptance tests (issues #7/#11)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, WorkerEvent, WorkerMessage, WorkerSession

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client):
    from django.contrib.auth.models import User

    user = User.objects.create_user("root", password="pw", is_staff=True)
    client.force_login(user)
    return client


@pytest.fixture
def worker(agent_clients):
    return services.provision_worker("worker-a", Agent.objects.get(name="alpha"))


def sync_payload(**overrides):
    payload = {
        "worker": "worker-a",
        "protocol_version": 1,
        "meta": {
            "version": "1.0.0",
            "hostname": "worker-home",
            "endpoint": "http://127.0.0.1:4096",
        },
        "sessions": [],
        "updates": [],
        "events": [],
        "results": [],
        "pending": {},
        "cursor": {},
    }
    payload.update(overrides)
    return payload


def post_sync(client, payload):
    return client.post("/api/worker/sync", payload, content_type="application/json")


class TestSyncContract:
    def test_requires_the_bound_agent_key(self, agent_clients, staff_client, worker):
        payload = sync_payload()
        assert post_sync(staff_client, payload).status_code == 403
        assert (
            Client().post("/api/worker/sync", payload, content_type="application/json").status_code
            == 401
        )
        # beta is not bound to worker-a
        assert post_sync(agent_clients[1], payload).status_code == 403

    def test_unsupported_protocol_version(self, api_client, worker):
        response = post_sync(api_client, sync_payload(protocol_version=99))
        assert response.status_code == 410
        assert "Unsupported sync protocol version" in response.json()["detail"]

    def test_missing_required_fields(self, api_client, worker):
        response = post_sync(api_client, {"worker": "worker-a"})
        assert response.status_code == 400

    def test_oversized_sections_are_413(self, api_client, worker):
        response = post_sync(
            api_client,
            sync_payload(events=[{"type": "e", "payload": {}} for _ in range(1001)]),
        )
        assert response.status_code == 413

    def test_oversized_content_length_is_413(self, api_client, worker, settings):
        settings.WORKER_SYNC_MAX_BYTES = 1000
        response = api_client.post("/api/worker/sync", b" " * 1001, content_type="application/json")
        assert response.status_code == 413


class TestSyncApplies:
    def test_full_batch_persists_observations(self, api_client, worker):
        payload = sync_payload(
            sessions=[
                {
                    "id": "ses_1",
                    "directory": "/repo",
                    "title": "T1",
                    "time": {"created": 100, "updated": 200},
                }
            ],
            updates=[
                {
                    "sessionID": "ses_1",
                    "messages": [
                        {
                            "info": {"id": "msg_1", "role": "user", "time": {"created": 100}},
                            "parts": [{"type": "text", "text": "hi"}],
                        }
                    ],
                }
            ],
            events=[
                {
                    "type": "session.status",
                    "sessionID": "ses_1",
                    "payload": {
                        "type": "session.status",
                        "properties": {"status": {"type": "busy"}},
                    },
                }
            ],
            cursor={"after": 0, "batch_id": "batch-1"},
        )
        response = post_sync(api_client, payload)
        assert response.status_code == 200
        body = response.json()
        assert body["acknowledged_batch_id"] == "batch-1"
        assert body["event_sequence"] == WorkerEvent.objects.get(worker=worker).pk
        assert body["epoch"] == str(services.sync_epoch())
        assert body["protocol_version"] == 1
        session = WorkerSession.objects.get(worker=worker, remote_id="ses_1")
        assert session.execution_status == "busy"
        assert WorkerMessage.objects.filter(session=session, in_last_window=True).count() == 1
        assert WorkerEvent.objects.filter(worker=worker).count() == 1

    def test_repeated_unacknowledged_batch_skips_events(self, api_client, worker):
        payload = sync_payload(
            sessions=[
                {
                    "id": "ses_1",
                    "directory": "/d",
                    "title": "T1",
                    "time": {"created": 1, "updated": 1},
                }
            ],
            events=[{"type": "custom.ping", "sessionID": "ses_1", "payload": {"n": 1}}],
            cursor={"after": 0, "batch_id": "batch-1"},
        )
        assert post_sync(api_client, payload).status_code == 200
        after_first = WorkerEvent.objects.filter(worker=worker).count()
        retry = post_sync(api_client, payload)
        assert retry.status_code == 200
        assert WorkerEvent.objects.filter(worker=worker).count() == after_first

    def test_new_batch_after_retry_applies(self, api_client, worker):
        base = sync_payload(
            events=[{"type": "custom.ping", "sessionID": "ses_1", "payload": {"n": 1}}],
            cursor={"after": 0, "batch_id": "batch-1"},
        )
        assert post_sync(api_client, base).status_code == 200
        follow_up = sync_payload(
            events=[{"type": "custom.ping", "sessionID": "ses_1", "payload": {"n": 2}}],
            cursor={"after": 1, "batch_id": "batch-2"},
        )
        assert post_sync(api_client, follow_up).status_code == 200
        assert WorkerEvent.objects.filter(worker=worker).count() == 2

    def test_unknown_session_update_rejects_whole_batch(self, api_client, worker):
        payload = sync_payload(
            sessions=[
                {
                    "id": "ses_1",
                    "directory": "/d",
                    "title": "T",
                    "time": {"created": 1, "updated": 1},
                }
            ],
            updates=[
                {
                    "sessionID": "ses_foreign",
                    "messages": [{"info": {"id": "m1", "role": "user", "time": {}}}],
                }
            ],
        )
        response = post_sync(api_client, payload)
        assert response.status_code == 400
        assert WorkerSession.objects.filter(worker=worker).count() == 0

    def test_stale_session_snapshot_never_regresses(self, api_client, worker):
        newer = sync_payload(
            sessions=[{"id": "ses_1", "title": "New", "time": {"created": 100, "updated": 200}}]
        )
        assert post_sync(api_client, newer).status_code == 200
        stale = sync_payload(
            sessions=[{"id": "ses_1", "title": "Old", "time": {"created": 100, "updated": 50}}]
        )
        assert post_sync(api_client, stale).status_code == 200
        assert WorkerSession.objects.get(worker=worker, remote_id="ses_1").title == "New"

    def test_error_and_busy_events_drive_last_error(self, api_client, worker):
        payload = sync_payload(
            sessions=[
                {
                    "id": "ses_1",
                    "directory": "/d",
                    "title": "T",
                    "time": {"created": 1, "updated": 1},
                }
            ],
            events=[
                {
                    "type": "session.error",
                    "sessionID": "ses_1",
                    "payload": {"properties": {"error": {"data": {"message": "boom"}}}},
                }
            ],
        )
        assert post_sync(api_client, payload).status_code == 200
        session = WorkerSession.objects.get(worker=worker, remote_id="ses_1")
        assert "boom" in session.last_error
        busy = sync_payload(
            events=[
                {
                    "type": "session.status",
                    "sessionID": "ses_1",
                    "payload": {"properties": {"status": {"type": "busy"}}},
                }
            ],
            cursor={"after": 1, "batch_id": "batch-9"},
        )
        assert post_sync(api_client, busy).status_code == 200
        session.refresh_from_db()
        assert session.last_error == "" and session.execution_status == "busy"

    def test_worker_meta_and_online_from_sync(self, api_client, worker):
        before = timezone.now()
        assert post_sync(api_client, sync_payload()).status_code == 200
        worker.refresh_from_db()
        assert worker.last_sync_at is not None and worker.last_sync_at >= before
        assert worker.opencode_version == "1.0.0"
        assert worker.opencode_url == "http://127.0.0.1:4096"
        assert worker.is_online()

    def test_sse_states_outside_inventory_window_do_not_reject_batch(self, api_client, worker):
        response = post_sync(
            api_client,
            sync_payload(
                events=[
                    {
                        "type": "session.error",
                        "sessionID": "ses_outside_window",
                        "payload": {"properties": {"error": "outside inventory"}},
                    },
                    {
                        "type": "session.status",
                        "sessionID": "ses_outside_window",
                        "payload": {"properties": {"status": {"type": "busy"}}},
                    },
                ]
            ),
        )
        assert response.status_code == 200
        assert WorkerEvent.objects.filter(worker=worker).count() == 2


class TestSyncEpoch:
    def test_epoch_is_durable_across_syncs(self, api_client, worker):
        first = post_sync(api_client, sync_payload())
        second = post_sync(api_client, sync_payload())
        assert first.json()["epoch"] == second.json()["epoch"]

    def test_regenerate_changes_epoch(self, api_client, worker):
        base = services.sync_epoch()
        services.regenerate_sync_epoch()
        response = post_sync(api_client, sync_payload())
        assert response.json()["epoch"] == str(services.sync_epoch())
        assert response.json()["epoch"] != base

    def test_resync_workers_command_changes_epoch(self, api_client, worker):
        from django.core.management import call_command

        base = services.sync_epoch()
        call_command("resync_workers")
        assert services.sync_epoch() != base

    def test_broker_failure_acts_like_broker_offline(self, api_client, worker, monkeypatch):
        def broken_layer():
            return None

        from coordinator import realtime

        monkeypatch.setattr(realtime, "get_channel_layer", broken_layer)
        assert post_sync(api_client, sync_payload()).status_code == 200


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Row-lock serialization requires PostgreSQL"
)
@pytest.mark.django_db(transaction=True)
class TestConcurrentSync:
    """CI's PostgreSQL job verifies two simultaneous syncs of one worker."""

    def test_same_worker_syncs_serialize(self, api_client, worker):
        barrier = Barrier(2)

        def run(count):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = post_sync(
                    Client(**api_client.defaults),
                    sync_payload(
                        events=[{"type": "custom.ping", "sessionID": "s", "payload": {"c": count}}],
                        cursor={"after": 0, "batch_id": f"batch-{count}"},
                    ),
                )
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(run, range(2))) == [200, 200]
        assert WorkerEvent.objects.filter(worker=worker).count() == 2
        assert set(
            WorkerEvent.objects.filter(worker=worker).values_list("payload__c", flat=True)
        ) == {0, 1}
