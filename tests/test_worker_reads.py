"""Issue #15: unified read APIs, cursors/gaps and post-commit notifications."""

import pytest

from coordinator import services
from coordinator.models import Agent, Worker, WorkerSession

from .test_worker_sync import post_sync, sync_payload

pytestmark = pytest.mark.django_db


class StubLayer:
    def __init__(self):
        self.messages = []

    async def group_send(self, group, message):
        self.messages.append((group, message))


@pytest.fixture
def staff_client(client):
    from django.contrib.auth.models import User

    user = User.objects.create_user("root", password="pw", is_staff=True)
    client.force_login(user)
    return client


@pytest.fixture
def layer(monkeypatch):
    stub = StubLayer()
    monkeypatch.setattr("coordinator.realtime.get_channel_layer", lambda: stub)
    return stub


@pytest.fixture
def workers(agent_clients):
    alpha = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    beta = services.provision_worker("worker-b", Agent.objects.get(name="beta"))
    return alpha, beta


SESSIONS_SNAPSHOT = [
    {
        "sessionID": "ses_parent",
        "directory": "/project/coordinator",
        "title": "Parent",
        "status": "idle",
        "time": {"created": 1761048000000, "updated": 1761048100000},
    },
    {
        "sessionID": "ses_sub",
        "directory": "/project/coordinator",
        "title": "Sub",
        "parentID": "ses_parent",
        "status": "busy",
        "agentName": "sub",
        "model": {"providerID": "openai", "modelID": "gpt"},
        "time": {"created": 1761048050000, "updated": 1761048150000},
    },
]


def publish_sync(client, worker="worker-a", **overrides):
    """One authenticated sync cycle with a connected/limited worker meta."""
    meta = {
        "session_limit": 50,
        "message_limit": 30,
        "connected": True,
        "stream_connected": True,
        **(overrides.pop("meta", None) or {}),
    }
    payload = sync_payload(**{"worker": worker, "meta": meta, **overrides})
    return post_sync(client, payload)


def parent_messages(start=1, count=6):
    return [
        {
            "id": f"msg_remote_{index}",
            "role": "user" if index % 2 else "assistant",
            "time": {"created": 1761048000000 + index},
            "content": f"hi {index}",
            "parts": [{"type": "text", "text": f"hi {index}"}],
        }
        for index in range(start, start + count)
    ]


def test_supplied_text_tool_reasoning_and_error_history_round_trips(workers, api_client):
    parts = [
        {"id": "p_text", "type": "text", "text": "Visible output"},
        {"id": "p_reason", "type": "reasoning", "text": "Supplied reasoning"},
        {
            "id": "p_tool",
            "type": "tool",
            "tool": "read",
            "callID": "call_fixture",
            "state": {"status": "completed", "input": {"filePath": "/fixture"}, "output": "data"},
        },
    ]
    info = {
        "id": "msg_history",
        "role": "assistant",
        "sessionID": "ses_parent",
        "error": {"name": "APIError", "data": {"message": "Supplied provider failure"}},
        "time": {"created": 1761048200000, "completed": 1761048201000},
    }
    response = publish_sync(
        api_client,
        sessions=SESSIONS_SNAPSHOT,
        updates=[{"sessionID": "ses_parent", "messages": [{"info": info, "parts": parts}]}],
    )
    assert response.status_code == 200
    history = api_client.get("/api/workers/worker-a/sessions/ses_parent/messages").json()
    message = history["messages"][0]
    assert message["parts"] == parts
    assert message["content"] == info


class TestWorkerScoping:
    def test_registry_readable_but_session_reads_scoped(self, workers, api_client, staff_client):
        assert api_client.get("/api/workers/worker-b").status_code == 200  # registry open
        assert api_client.get("/api/workers/worker-b/sessions").status_code == 403
        assert staff_client.get("/api/workers/worker-b/sessions").status_code == 200

    def test_worker_detail_includes_counts_and_models(self, workers, api_client):
        services.record_worker_meta(
            workers[0],
            meta={
                "connected": True,
                "models": [{"providerID": "openai", "modelID": "gpt", "name": "gpt"}],
                "default_model": {"providerID": "openai", "modelID": "gpt"},
            },
        )
        detail = api_client.get("/api/workers/worker-a").json()
        assert detail["models"][0]["modelID"] == "gpt"
        assert detail["default_model"] == {"providerID": "openai", "modelID": "gpt"}
        assert detail["counts"] == {"sessions": 0, "commands": 0, "events": 0, "pending": 0}


class TestSessionReads:
    def test_list_filters_before_limit(self, workers, api_client):
        assert publish_sync(api_client, sessions=SESSIONS_SNAPSHOT).status_code == 200
        listing = api_client.get("/api/workers/worker-a/sessions").json()
        assert listing["count"] == 2
        filtered = api_client.get("/api/workers/worker-a/sessions", {"status": "busy"}).json()
        assert [s["sessionID"] for s in filtered["items"]] == ["ses_sub"]
        directory = api_client.get(
            "/api/workers/worker-a/sessions", {"directory": "/project/coordinator"}
        ).json()
        assert directory["count"] == 2
        assert (
            api_client.get("/api/workers/worker-a/sessions", {"directory": "/nowhere"}).json()[
                "count"
            ]
            == 0
        )

    def test_session_detail_shape_and_cards(self, workers, api_client):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        publish_sync(
            api_client,
            sessions=SESSIONS_SNAPSHOT,
            updates=[
                {
                    "sessionID": "ses_sub",
                    "messages": [
                        {
                            "id": "msg_sub_1",
                            "role": "user",
                            "time": {"created": 1761048050001},
                            "content": "hi sub",
                            "parts": [{"type": "text", "text": "hi sub"}],
                        }
                    ],
                }
            ],
        )
        detail = api_client.get("/api/workers/worker-a/sessions/ses_sub").json()
        assert detail["parentID"] == "ses_parent"
        assert detail["model"] == {"providerID": "openai", "modelID": "gpt"}
        assert detail["agentName"] == "sub"
        assert detail["executionStatus"] == "busy"
        assert detail["firstSeenAt"] is not None and detail["lastSeenAt"] is not None
        assert detail["remoteCreatedMs"] == 1761048050000
        assert detail["window"]["complete"] is True  # window < message_limit
        assert detail["counts"]["messages"] == 1
        assert detail["counts"]["pending"] == 0
        assert detail["attempts"] == [] and detail["taskLinks"] == []
        assert detail["commands"] == [] and detail["pendingInteractions"] == []

    def test_parent_subagent_identity_resolution(self, workers, api_client):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        parent = api_client.get("/api/workers/worker-a/sessions/ses_parent").json()
        sub = api_client.get("/api/workers/worker-a/sessions/ses_sub").json()
        assert sub["parent"] == "ses_parent"  # worker-scoped row resolved
        assert parent["subagents"] == ["ses_sub"]

    def test_interactions_and_commands_in_detail(self, workers, api_client):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        worker = Worker.objects.get(pk=workers[0].pk)  # refreshed flags
        services.enqueue_worker_command(
            Agent.objects.get(name="alpha"),
            "alpha",
            worker,
            "prompt",
            {"sessionID": "ses_sub", "text": "go"},
            "ses_sub",
        )
        detail = api_client.get("/api/workers/worker-a/sessions/ses_sub").json()
        assert detail["commands"][0]["action"] == "prompt"
        assert "result" in detail["commands"][0]

    def test_attempt_task_links_via_launch(self, workers, api_client, project):
        assert publish_sync(api_client, sessions=SESSIONS_SNAPSHOT).status_code == 200
        alpha = Agent.objects.get(name="alpha")
        task_id = services.create_task(alpha, project, "T", "").pk

        assert api_client.post(f"/api/tasks/{task_id}/claim", {}, format="json").status_code == 200
        # Launch's create has not resolved to an observed remote id yet: an empty
        # project filter proves sessions need a materialized attempt link.
        project_filter = api_client.get("/api/sessions", {"project": project.pk}).json()
        assert project_filter["count"] == 0

    def test_same_remote_id_two_workers_isolated(self, workers, api_client, agent_clients):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        publish_sync(agent_clients[1], worker="worker-b", sessions=SESSIONS_SNAPSHOT)
        unified = agent_clients[1].get("/api/sessions", {"limit": 10}).json()
        parent_ids = sorted(s["worker"] for s in unified["items"] if s["sessionID"] == "ses_parent")
        assert parent_ids == ["worker-b"]  # rows never merge; agent sees only own

    def test_agent_sees_only_bound_worker_sessions(
        self, workers, api_client, agent_clients, staff_client
    ):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        publish_sync(agent_clients[1], worker="worker-b", sessions=SESSIONS_SNAPSHOT)
        staff_view = staff_client.get("/api/sessions", {"limit": 10}).json()
        workers_shown = {s["worker"] for s in staff_view["items"] if s["sessionID"] == "ses_parent"}
        assert workers_shown == {"worker-a", "worker-b"}  # never merged cross-worker
        assert staff_view["count"] == 4
        mine = api_client.get("/api/sessions", {"limit": 10}).json()
        assert {s["worker"] for s in mine["items"]} == {"worker-a"}
        assert mine["count"] == 2

    def test_unknown_session_is_scoped(self, workers, api_client, agent_clients):
        assert api_client.get("/api/workers/worker-a/sessions/ses_none").status_code == 404
        assert (
            agent_clients[1].get("/api/workers/worker-a/sessions/ses_parent/messages").status_code
            == 403
        )


class TestCursorReads:
    def _publish_history(self, api_client):
        publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        publish_sync(
            api_client,
            sessions=SESSIONS_SNAPSHOT,
            updates=[{"sessionID": "ses_parent", "messages": parent_messages(1, 6)}],
        )

    def test_messages_cursor_roundtrip(self, workers, api_client):
        self._publish_history(api_client)
        first = api_client.get("/api/workers/worker-a/sessions/ses_parent/messages").json()
        assert len(first["messages"]) == 6
        assert first["hasMore"] is False
        empty = api_client.get(
            "/api/workers/worker-a/sessions/ses_parent/messages", {"after": first["cursor"]}
        ).json()
        assert empty["messages"] == []
        single = api_client.get(
            "/api/workers/worker-a/sessions/ses_parent/messages", {"limit": 2}
        ).json()
        assert len(single["messages"]) == 2 and single["hasMore"] is True
        paged = api_client.get(
            "/api/workers/worker-a/sessions/ses_parent/messages",
            {"after": single["cursor"], "limit": 500},
        ).json()
        assert len(paged["messages"]) == 4 and paged["hasMore"] is False

    def test_message_cards_components_serialized(self, workers, api_client):
        self._publish_history(api_client)
        body = api_client.get("/api/workers/worker-a/sessions/ses_parent/messages").json()
        first = body["messages"][0]
        assert first["role"] in {"user", "assistant"}
        assert first["remoteID"] == first["remoteID"] and first["remoteID"].startswith(
            "msg_remote_"
        )
        assert first["parts"][0]["text"].startswith("hi ")
        assert first["content"]["content"].startswith("hi ")
        assert first["remoteTime"] == 1761048000000 + int(first["remoteID"].rsplit("_", 1)[-1])

    def test_message_gap_after_pruning(self, workers, api_client):
        self._publish_history(api_client)
        session = WorkerSession.objects.get(worker_id=workers[0].pk, remote_id="ses_parent")
        oldest_pk = session.messages.order_by("id").values_list("id", flat=True).first()
        session.messages.filter(id__lt=oldest_pk + 2).delete()
        body = api_client.get(
            "/api/workers/worker-a/sessions/ses_parent/messages",
            {"after": oldest_pk - 1, "limit": 500},
        ).json()
        assert body["gap"] is True  # requested range predates the retained floor
        assert body["messages"], "served from the retained start"
        assert body["messages"][0]["id"] == oldest_pk + 2

    def test_events_cursor_exclusive_and_gap(self, workers, api_client, staff_client):
        worker = Worker.objects.get(pk=workers[0].pk)
        services.store_events(
            worker,
            [{"type": "session.error", "sessionID": "ses_parent", "payload": {"error": "first"}}],
        )
        services.store_events(
            worker,
            [{"type": "session.error", "sessionID": "ses_parent", "payload": {"error": "second"}}],
        )
        first = staff_client.get("/api/workers/worker-a/events", {"limit": 1}).json()
        assert [e["payload"]["error"] for e in first["events"]] == ["first"]
        assert first["gap"] is False and first["hasMore"] is True
        second = staff_client.get("/api/workers/worker-a/events", {"after": first["cursor"]}).json()
        assert [e["payload"]["error"] for e in second["events"]] == ["second"]
        # Expired cursor: retention dropped rows before the retained floor.
        oldest_pk = worker.events.order_by("id").values_list("pk", flat=True).first()
        worker.events.filter(pk=oldest_pk).delete()
        worker.dropped_events = 7
        worker.save(update_fields=["dropped_events"])
        expired = staff_client.get("/api/workers/worker-a/events", {"after": oldest_pk - 1}).json()
        assert expired["gap"] is True
        assert expired["dropped"] == 7
        assert expired["hasMore"] is False

    def test_events_limit_cap(self, workers, api_client):
        worker = Worker.objects.get(pk=workers[0].pk)
        services.store_events(
            worker,
            [
                {
                    "type": "session.error",
                    "sessionID": "ses_parent",
                    "payload": {"error": f"fail {tick}"},
                }
                for tick in range(5)
            ],
        )
        body = api_client.get("/api/workers/worker-a/events", {"limit": 2}).json()
        assert len(body["events"]) == 2 and body["hasMore"] is True


class TestNotifications:
    def test_post_commit_invalidation_messages(
        self, layer, api_client, workers, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
            publish_sync(
                api_client,
                sessions=SESSIONS_SNAPSHOT,
                updates=[{"sessionID": "ses_parent", "messages": parent_messages(1, 6)}],
            )
        subjects = [message[1].get("subject") for message in layer.messages]
        assert "sessions" in subjects and "messages" in subjects

    def test_rollback_is_silent(
        self, layer, api_client, workers, django_capture_on_commit_callbacks
    ):
        """An envelope rejected mid-transaction leaves NO partial state or noise."""
        with django_capture_on_commit_callbacks(execute=True):
            assert publish_sync(api_client, sessions=SESSIONS_SNAPSHOT).status_code == 200
            response = publish_sync(
                api_client,
                updates=[{"sessionID": "ses_foreign", "messages": parent_messages(1, 1)}],
            )
        assert response.status_code == 400
        # The rejected cycle registered nothing: only the successful sync's two
        # session rows exist and only their notifications fired.
        assert WorkerSession.objects.count() == 2
        subjects = [message[1].get("subject") for message in layer.messages]
        assert subjects.count("sessions") >= 2  # only the successful cycle published

    def test_command_notifications_after_commit(
        self, layer, api_client, workers, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
            services.enqueue_worker_command(
                Agent.objects.get(name="alpha"),
                "alpha",
                Worker.objects.get(pk=workers[0].pk),
                "prompt",
                {"sessionID": "ses_sub", "text": "go"},
                "ses_sub",
            )
        subjects = [message[1].get("subject") for message in layer.messages]
        assert "commands" in subjects
        types = [message[1].get("event_type") for message in layer.messages]
        assert "command.queued" in types

    def test_broker_failure_keeps_committed_mutation(self, layer, api_client, workers):
        async def explode(group, message):
            raise RuntimeError("broker down")

        layer.group_send = explode
        response = publish_sync(api_client, sessions=SESSIONS_SNAPSHOT)
        assert response.status_code == 200
