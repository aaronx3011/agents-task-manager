import pytest

from coordinator import services
from coordinator.models import Agent, WorkerMessage

pytestmark = pytest.mark.django_db


@pytest.fixture
def history_worker(agent_clients):
    worker = services.provision_worker("history-worker", Agent.objects.get(name="alpha"))
    services.store_sessions(worker, [{"id": "ses_history", "directory": "/fixture"}])
    services.record_worker_meta(worker, {"connected": True, "stream_connected": True})
    return worker


def messages(start, count, same_time=False):
    return [
        {
            "info": {
                "id": f"msg_{number}",
                "role": "assistant",
                "time": {"created": 1 if same_time else number},
            },
            "parts": [{"type": "text", "text": str(number)}],
        }
        for number in range(start, start + count)
    ]


PATH = "/api/workers/history-worker/sessions/ses_history/messages"


def test_latest_ten_then_exclusive_older_pages_survive_out_of_order_backfill(
    history_worker, api_client
):
    services.store_messages(history_worker, "ses_history", messages(25, 10))
    services.store_messages(history_worker, "ses_history", messages(0, 25))
    page = api_client.get(PATH, {"latest": "true", "limit": 10}).json()
    assert [row["remoteID"] for row in page["messages"]] == [f"msg_{i}" for i in range(25, 35)]
    assert page["hasMore"] and page["olderCursor"]
    seen = [row["remoteID"] for row in page["messages"]]
    sizes = [len(seen)]
    while page["olderCursor"]:
        response = api_client.get(PATH, {"before": page["olderCursor"], "limit": 10})
        assert response.status_code == 200
        page = response.json()
        sizes.append(len(page["messages"]))
        seen = [row["remoteID"] for row in page["messages"]] + seen
    assert sizes == [10, 10, 10, 5]
    assert seen == [f"msg_{i}" for i in range(35)]
    assert not page["hasMore"]


def test_equal_timestamps_have_a_stable_tie_breaker_and_latest_is_bounded(
    history_worker, api_client
):
    services.store_messages(history_worker, "ses_history", messages(0, 25, same_time=True))
    page = api_client.get(PATH, {"latest": "true", "limit": 500}).json()
    assert len(page["messages"]) == 10
    older = api_client.get(PATH, {"before": page["olderCursor"]}).json()
    assert len(older["messages"]) == 10
    newest = {row["remoteID"] for row in page["messages"]}
    page_two = {row["remoteID"] for row in older["messages"]}
    assert not newest & page_two


@pytest.mark.parametrize("cursor", ["bad", "1", "1:0", "1:2:3", "1:9223372036854775808"])
def test_malformed_before_cursors_are_rejected(history_worker, api_client, cursor):
    assert api_client.get(PATH, {"before": cursor}).status_code == 400


def test_history_retention_gap_is_explicit_and_forward_clients_are_unchanged(
    history_worker, api_client
):
    services.store_messages(history_worker, "ses_history", messages(1, 25))
    initial = api_client.get(PATH, {"latest": "true"}).json()
    time, pk = initial["olderCursor"].split(":")
    WorkerMessage.objects.filter(pk=pk).delete()
    page = api_client.get(PATH, {"before": f"{time}:{pk}"}).json()
    assert page["gap"]
    legacy = api_client.get(PATH, {"limit": 2}).json()
    assert [row["remoteID"] for row in legacy["messages"]] == ["msg_1", "msg_2"]
    assert "olderCursor" not in legacy
    assert api_client.get(PATH, {"latest": "true", "after": 1}).status_code == 400


def test_latest_history_still_requires_the_owning_worker_key(history_worker, agent_clients):
    assert agent_clients[1].get(PATH, {"latest": "true"}).status_code == 403


def test_compact_session_details_omit_command_history_but_keep_reply_status(
    history_worker, api_client
):
    services.enqueue_worker_command(
        None,
        "fixture",
        history_worker,
        "prompt",
        {"text": "not chat history"},
        "ses_history",
    )
    services.ingest_pending_interactions(
        history_worker,
        {
            "complete": True,
            "permissions": [
                {"id": "per_history", "sessionID": "ses_history", "title": "Permission"}
            ],
            "questions": [],
        },
    )
    reply, _ = services.enqueue_worker_command(
        None,
        "fixture",
        history_worker,
        "permission.reply",
        {"requestID": "per_history", "reply": "once"},
        "ses_history",
    )
    path = "/api/workers/history-worker/sessions/ses_history"
    compact = api_client.get(path, {"compact": "true"}).json()
    assert [row["id"] for row in compact["commands"]] == [str(reply.pk)]
    assert compact["commands"][0]["payload"] == {}
    assert "not chat history" not in str(compact)
    assert len(api_client.get(path).json()["commands"]) == 2
