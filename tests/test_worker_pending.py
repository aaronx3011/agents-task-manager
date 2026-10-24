"""Pending permissions/questions sync and reply commands (issue #14)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, PendingInteraction, WorkerCommand, WorkerSession

pytestmark = pytest.mark.django_db


@pytest.fixture
def online_worker(agent_clients):
    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    worker.connected = True
    worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    return worker


@pytest.fixture
def sessions(online_worker):
    return services.store_sessions(
        online_worker,
        [
            {"id": "ses_1", "directory": "/repo", "title": "T1"},
            {"id": "ses_9", "directory": "/repo", "title": "Older session outside window"},
        ],
    )


PERMISSION = {"id": "per_a", "sessionID": "ses_1", "type": "file.edit", "title": "Allow edit"}
QUESTION = {
    "id": "que_a",
    "sessionID": "ses_1",
    "questions": [
        {
            "header": "Color",
            "text": "Pick a color",
            "options": [
                {"label": "Blue", "description": "b"},
                {"label": "Red", "description": "r"},
            ],
            "multiple": True,
        },
        {
            "header": "Strict",
            "text": "Fixed options only",
            "options": [{"label": "A"}, {"label": "B"}],
            "custom": False,
        },
    ],
}


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


def post_command(client, payload):
    return client.post("/api/commands", payload, content_type="application/json")


def permission_replied(key="per_a", session="ses_1", reply="once"):
    return {
        "worker": "worker-a",
        "action": "permission.reply",
        "sessionID": session,
        "payload": {"requestID": key, "reply": reply},
    }


def question_replied(answers=None):
    return {
        "worker": "worker-a",
        "action": "question.reply",
        "sessionID": "ses_1",
        "payload": {"requestID": "que_a", "answers": answers or [["Blue"], ["A"]]},
    }


class TestSnapshotIngest:
    def test_authoritative_snapshot_creates_links(self, api_client, online_worker, sessions):
        response = post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": [QUESTION]}},
        )
        assert response.status_code == 200
        worker_rows = PendingInteraction.objects.filter(worker=online_worker)
        assert worker_rows.count() == 2
        permission = worker_rows.get(kind="permission", request_id="per_a")
        question = worker_rows.get(kind="question", request_id="que_a")
        assert permission.session.remote_id == "ses_1"
        assert question.data["questions"][0]["multiple"] is True
        # Older session requests survive retention because raw ids persist.
        read = services.worker_read_model(PendingInteraction.objects.first().worker, counts=True)
        assert read["counts"]["pending"] == 2

    def test_incomplete_batch_changes_nothing(self, api_client, online_worker, sessions):
        post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": []}},
        )
        assert PendingInteraction.objects.count() == 1
        # Bridge-side fetch failure: incomplete batch, no snapshot rights.
        response = post_sync(
            api_client, {"pending": {"complete": False, "permissions": [], "questions": []}}
        )
        assert response.status_code == 200
        assert PendingInteraction.objects.count() == 1
        online_worker.refresh_from_db()

    def test_resolved_only_from_authoritative_snapshot(self, api_client, online_worker, sessions):
        post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": [QUESTION]}},
        )
        # Another client (TUI) answered: complete snapshot omits both requests.
        response = post_sync(
            api_client, {"pending": {"complete": True, "permissions": [], "questions": []}}
        )
        assert response.status_code == 200
        assert PendingInteraction.objects.filter(resolved_at=None).count() == 0
        rows = list(PendingInteraction.objects.all())
        assert all(row.resolution == "snapshot" for row in rows)
        # New appearance of the same request re-opens it.
        post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": []}},
        )
        assert PendingInteraction.objects.get(request_id="per_a").resolved_at is None

    def test_requests_on_older_sessions_kept(self, api_client, online_worker, sessions, settings):
        settings.WORKER_SESSION_KEEP = 1
        post_sync(
            api_client,
            {
                "pending": {
                    "complete": True,
                    "permissions": [{**PERMISSION, "sessionID": "ses_9"}],
                    "questions": [],
                }
            },
        )
        # POC parity: sessions with open pending requests stay outside the window
        # but remain observable until the request resolves authoritatively.
        services.apply_observation_retention()
        assert WorkerSession.objects.count() == 2
        assert services.worker_read_model(online_worker, counts=True)["counts"]["pending"] == 1
        post_sync(api_client, {"pending": {"complete": True, "permissions": [], "questions": []}})
        services.apply_observation_retention()
        assert WorkerSession.objects.count() == 1  # snapshot resolved it; retention applies
        assert PendingInteraction.objects.get(request_id="per_a").resolved_at is not None


class TestReplies:
    def _seed(self, api_client, online_worker, sessions):
        post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": [QUESTION]}},
        )
        return PendingInteraction.objects.get(request_id="per_a"), PendingInteraction.objects.get(
            request_id="que_a"
        )

    def test_permission_reply_enqueues_and_links(self, api_client, online_worker, sessions):
        permission, _ = self._seed(api_client, online_worker, sessions)
        response = post_command(api_client, permission_replied())
        assert response.status_code == 202
        card = response.json()
        assert card["payload"] == {"reply": "once"}
        permission.refresh_from_db()
        assert permission.reply_command_id == int(card["id"])
        # Second active reply conflicts.
        duplicate = post_command(api_client, permission_replied(reply="reject"))
        assert duplicate.status_code == 409

    def test_invalid_permission_reply(self, api_client, online_worker, sessions):
        self._seed(api_client, online_worker, sessions)
        response = post_command(api_client, permission_replied(reply="always-with-feedback"))
        assert response.status_code == 400
        good = post_command(
            api_client,
            {
                "worker": "worker-a",
                "action": "permission.reply",
                "sessionID": "ses_1",
                "payload": {"requestID": "per_a", "reply": "once", "message": "ok"},
            },
        )
        assert good.status_code == 202

    def test_question_reply_rules(self, api_client, online_worker, sessions):
        self._seed(api_client, online_worker, sessions)
        wrong_count = post_command(api_client, question_replied(answers=[["Blue"]]))
        assert wrong_count.status_code == 400
        multiple_answered = post_command(
            api_client, question_replied(answers=[["Blue", "Red"], ["A"]])
        )
        assert multiple_answered.status_code == 202
        # Second reply for a request already being delivered conflicts.
        again = post_command(api_client, question_replied(answers=[["Red"], ["A"]]))
        assert again.status_code == 409

    def test_single_and_custom_restrictions(self, api_client, online_worker, sessions):
        self._seed(api_client, online_worker, sessions)
        single = post_command(api_client, question_replied(answers=[["Blue"], ["A", "B"]]))
        assert single.status_code == 400  # question 2 forbids multiple without multiple flag
        custom = post_command(api_client, question_replied(answers=[["Blue"], ["C"]]))
        assert custom.status_code == 400  # custom=false prohibits off-menu answers

    def test_reject_and_foreign_request(self, api_client, online_worker, sessions):
        self._seed(api_client, online_worker, sessions)
        reject = post_command(
            api_client,
            {
                "worker": "worker-a",
                "action": "question.reject",
                "sessionID": "ses_1",
                "payload": {"requestID": "que_a"},
            },
        )
        assert reject.status_code == 202
        foreign = post_command(
            api_client,
            {
                "worker": "worker-a",
                "action": "question.reject",
                "sessionID": "ses_9",
                "payload": {"requestID": "que_a"},
            },
        )
        assert foreign.status_code == 404

    def test_reply_error_does_not_resolve(self, api_client, online_worker, sessions):
        self._seed(api_client, online_worker, sessions)
        response = post_command(api_client, permission_replied()).json()
        post_sync(api_client)
        post_sync(
            api_client,
            {
                "results": [
                    {
                        "commandID": response["id"],
                        "status": "error",
                        "error": "request is gone locally",
                    }
                ]
            },
        )
        interaction = PendingInteraction.objects.get(request_id="per_a")
        # A failed reply never silently resolves; the authoritative snapshot owns it.
        assert interaction.resolved_at is None
        command = WorkerCommand.objects.get(pk=int(response["id"]))
        assert command.status == "error"


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Concurrent replies require PostgreSQL"
)
@pytest.mark.django_db(transaction=True)
class TestConcurrentReplies:
    def test_single_active_reply_per_request(self, api_client, online_worker, sessions):
        post_sync(
            api_client,
            {"pending": {"complete": True, "permissions": [PERMISSION], "questions": []}},
        )
        barrier = Barrier(2)

        def run(idx):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return post_command(Client(**api_client.defaults), permission_replied()).status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(run, range(2)))
        assert sorted(statuses) == [202, 409]
        assert WorkerCommand.objects.filter(action="permission.reply").count() == 1
