import pytest
from django.core.management import call_command
from django.utils import timezone

from coordinator import services
from coordinator.models import Agent, WorkerEvent, WorkerMessage, WorkerSession

pytestmark = pytest.mark.django_db


@pytest.fixture
def workers(agent_clients):
    a = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    b = services.provision_worker("worker-b", Agent.objects.get(name="beta"))
    return a, b


def event_record(event_type, session_id, payload):
    return {"type": event_type, "sessionID": session_id, "data": payload}


def test_same_remote_id_stays_isolated_per_worker(workers):
    worker_a, worker_b = workers
    first = services.store_sessions(
        worker_a, [{"id": "ses_shared", "directory": "/a", "title": "A"}]
    )
    second = services.store_sessions(
        worker_b, [{"id": "ses_shared", "directory": "/b", "title": "B"}]
    )
    assert first[0].pk != second[0].pk
    assert WorkerSession.objects.count() == 2
    assert first[0].title == "A" and second[0].title == "B"


def test_message_upsert_replaces_streaming_content_not_duplicates(workers):
    worker_a, _ = workers
    (session,) = services.store_sessions(worker_a, [{"id": "ses_s", "directory": "/a"}])
    base = {
        "info": {"id": "msg_1", "role": "user", "time": {"created": 1000}},
        "parts": [{"type": "text", "text": "hi"}],
    }
    services.store_messages(worker_a, "ses_s", [base])
    streaming = {
        "info": {"id": "msg_1", "role": "user", "time": {"created": 1000}},
        "parts": [{"type": "text", "text": "hi"}, {"type": "step-start"}],
    }
    session = services.store_messages(
        worker_a,
        "ses_s",
        [
            streaming,
            {"info": {"id": "msg_2", "role": "assistant", "time": {"created": 2000}}, "parts": []},
        ],
    )
    rows = list(WorkerMessage.objects.filter(session=session).order_by("remote_id"))
    assert [row.remote_id for row in rows] == ["msg_1", "msg_2"]
    assert len(rows[0].parts) == 2  # streaming replaced content in place
    assert session.window_count == 2 and session.window_oldest_id == "msg_2"
    assert session.window_complete  # 2 < default message_limit 50


def test_missing_window_entries_are_not_deletions(workers):
    worker_a, _ = workers
    (session,) = services.store_sessions(worker_a, [{"id": "ses_s", "directory": "/a"}])
    messages = [
        {"info": {"id": f"msg_{n}", "role": "user", "time": {"created": n}}, "parts": []}
        for n in range(1, 4)
    ]
    services.store_messages(worker_a, "ses_s", messages)
    services.store_messages(
        worker_a,
        "ses_s",
        [{"info": {"id": "msg_4", "role": "user", "time": {"created": 4}}, "parts": []}],
    )
    assert WorkerMessage.objects.filter(session=session).count() == 4
    stale = WorkerMessage.objects.get(session=session, remote_id="msg_1")
    assert stale.in_last_window is False
    assert WorkerMessage.objects.get(session=session, remote_id="msg_4").in_last_window


def test_parent_subagent_links_resolve_after_late_parent_observation(workers):
    worker_a, _ = workers
    (child,) = services.store_sessions(
        worker_a, [{"id": "ses_child", "directory": "/a", "parentID": "ses_parent"}]
    )
    assert child.parent_id is None and child.remote_parent_id == "ses_parent"
    (parent,) = services.store_sessions(
        worker_a, [{"id": "ses_parent", "directory": "/a", "title": "Parent"}]
    )
    child = WorkerSession.objects.get(pk=child.pk)
    assert child.parent_id == parent.pk
    assert parent.subagents.count() == 1
    # A cross-worker parent never links: each worker resolves only its own rows.
    worker_b = workers[1]
    (b_child,) = services.store_sessions(
        worker_b, [{"id": "ses_child", "directory": "/b", "parentID": "ses_parent"}]
    )
    assert b_child.parent_id is None
    (b_parent,) = services.store_sessions(worker_b, [{"id": "ses_parent", "directory": "/b"}])
    b_child_row = WorkerSession.objects.get(worker=worker_b, remote_id="ses_child")
    a_child_row = WorkerSession.objects.get(worker=worker_a, remote_id="ses_child")
    assert b_child_row.parent_id == b_parent.pk
    assert a_child_row.parent_id == parent.pk


def test_snapshot_regression_is_rejected(workers):
    worker_a, _ = workers
    (session,) = services.store_sessions(
        worker_a,
        [
            {
                "id": "ses_s",
                "directory": "/a",
                "title": "Newer",
                "time": {"created": 1, "updated": 200},
            }
        ],
    )
    services.store_sessions(
        worker_a,
        [
            {
                "id": "ses_s",
                "directory": "/a",
                "title": "Older",
                "time": {"created": 1, "updated": 100},
            }
        ],
    )
    session.refresh_from_db()
    assert session.title == "Newer" and session.remote_updated_ms == 200


def test_execution_error_persists_until_new_execution(workers):
    worker_a, _ = workers
    (session,) = services.store_sessions(worker_a, [{"id": "ses_s", "directory": "/a"}])
    services.record_execution_error(session, "provider DID fail")
    session.refresh_from_db()
    assert "provider DID fail" in session.last_error and session.last_error_at is not None
    services.clear_execution_error(session)
    session.refresh_from_db()
    assert session.last_error == "" and session.last_error_at is None


def test_event_dedupe_cursor_and_truncation(workers, settings):
    settings.WORKER_EVENT_MAX_BYTES = 200
    worker_a, worker_b = workers
    payload = {"properties": {"x": "y" * 400}}
    first = services.store_events(worker_a, [event_record("session.idle", "ses_e", payload)])
    second = services.store_events(worker_a, [event_record("session.idle", "ses_e", payload)])
    third = services.store_events(worker_b, [event_record("session.idle", "ses_e", payload)])
    assert len(first) == len(third) == 1 and second == []  # hash dedupe, worker scoped
    row = first[0]
    assert row.payload["truncated"] is True and "preview" in row.payload
    # Stable cursor ordering: later insertions have strictly higher sequence.
    follow = services.store_events(
        worker_a, [event_record("session.error", "ses_e", {"properties": {"error": "boom"}})]
    )
    assert follow[0].pk > row.pk
    assert follow[0].event_type == "session.error"
    assert first[0].session is None  # session unknown; remote id preserved


def test_event_links_known_session(workers):
    worker_a, _ = workers
    (session,) = services.store_sessions(worker_a, [{"id": "ses_link", "directory": "/a"}])
    (event,) = services.store_events(worker_a, [event_record("session.idle", "ses_link", {})])
    assert event.session_id == session.pk and event.remote_session_id == "ses_link"


def test_event_ring_retention(workers, settings):
    settings.WORKER_EVENT_KEEP = 5
    worker_a, _ = workers
    for number in range(8):
        services.record_worker_meta(worker_a, meta={})
        services.store_events(
            worker_a, [event_record("session.tick", "ses_x", {"properties": {"n": number}})]
        )
    assert WorkerEvent.objects.filter(worker=worker_a).count() == 5
    remaining = list(WorkerEvent.objects.filter(worker=worker_a).order_by("pk"))
    assert [row.payload["properties"]["n"] for row in remaining] == [3, 4, 5, 6, 7]


def test_retention_command_prunes_but_keeps_recent(workers, settings):
    settings.WORKER_SESSION_KEEP = 1
    settings.WORKER_MESSAGE_KEEP = 2
    worker_a, _ = workers
    now = timezone.now()
    old, _new = services.store_sessions(
        worker_a,
        [
            {"id": "ses_old", "directory": "/a", "title": "old"},
            {"id": "ses_new", "directory": "/a", "title": "new"},
        ],
    )
    WorkerSession.objects.filter(pk=old.pk).update(last_seen_at=now - timezone.timedelta(days=30))
    (fresh,) = services.store_sessions(
        worker_a, [{"id": "ses_fresh", "directory": "/a", "title": "fresh"}]
    )
    for n in range(5):
        services.store_messages(
            worker_a,
            "ses_fresh",
            [{"info": {"id": f"m{n}", "role": "user", "time": {"created": n}}, "parts": []}],
        )
    removed = services.apply_observation_retention()
    assert removed["sessions"] == 2 and removed["messages"] == 3
    assert WorkerSession.objects.filter(worker=worker_a, remote_id="ses_old").exists() is False
    assert WorkerSession.objects.filter(worker=worker_a, remote_id="ses_new").exists() is False
    assert WorkerMessage.objects.filter(session=fresh).count() == 2
    call_command("prune_worker_data")  # command surface


def test_persistence_after_restart(workers):
    worker_a, _ = workers
    services.store_sessions(worker_a, [{"id": "ses_m", "directory": "/a", "title": "kept"}])
    services.store_messages(
        worker_a,
        "ses_m",
        [
            {
                "info": {"id": "msg_x", "role": "assistant", "time": {"created": 9}},
                "parts": [{"type": "text", "text": "hi"}],
            }
        ],
    )
    # Fresh DB session/state: read through new queries like a restarted process.
    stored = WorkerSession.objects.get(worker=worker_a, remote_id="ses_m")
    again = WorkerMessage.objects.get(session=stored, remote_id="msg_x")
    assert again.parts == [{"type": "text", "text": "hi"}]
