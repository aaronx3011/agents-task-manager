"""Real ASGI protocol tests: live observations never persist message chunks."""

import json

import anyio
import pytest
from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.core.cache import cache, caches
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from config.asgi import application
from coordinator import services
from coordinator.live import snapshot_key
from coordinator.models import Agent, Worker, WorkerMessage

from .test_dashboard import connection as browser_connection
from .test_worker_sync import post_sync, sync_payload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def live_worker(agent_clients):
    cache.clear()
    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    services.store_sessions(worker, [{"id": "ses_shared", "directory": "/repo"}])
    return worker, agent_clients[0].defaults["HTTP_AUTHORIZATION"].split()[1]


@pytest.fixture
def live_staff(django_user_model):
    client = Client()
    client.force_login(django_user_model.objects.create_user("live-human", is_staff=True))
    return client


def worker_connection():
    return ApplicationCommunicator(
        application,
        {
            "type": "websocket",
            "path": "/ws/worker/",
            "raw_path": b"/ws/worker/",
            "headers": [(b"host", b"localhost")],
            "query_string": b"",
            "scheme": "ws",
            "subprotocols": [],
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 1),
        },
    )


async def send(socket, data):
    await socket.send_input({"type": "websocket.receive", "text": json.dumps(data)})


async def receive(socket):
    return json.loads((await socket.receive_output(timeout=5))["text"])


async def open_worker(key, name="worker-a"):
    socket = worker_connection()
    await socket.send_input({"type": "websocket.connect"})
    assert (await socket.receive_output())["type"] == "websocket.accept"
    await send(socket, {"type": "authenticate", "worker": name, "key": key})
    return socket


async def close(socket):
    await socket.send_input({"type": "websocket.disconnect", "code": 1000})
    await socket.wait(timeout=5)


def message(text):
    return {
        "info": {"id": "msg_1", "sessionID": "ses_shared", "role": "assistant"},
        "parts": [{"type": "text", "text": text}],
    }


def test_live_focus_cache_isolation_and_explicit_batch_persistence(
    live_worker,
    live_staff,
    api_client,
    agent_clients,
):
    worker, key = live_worker
    other = services.provision_worker("worker-b", Agent.objects.get(name="beta"))
    services.store_sessions(other, [{"id": "ses_shared", "directory": "/other"}])
    other_key = agent_clients[1].defaults["HTTP_AUTHORIZATION"].split()[1]

    async def verify():
        producer = await open_worker(key)
        assert (await receive(producer))["type"] == "ready"
        browser = browser_connection(live_staff)
        await browser.send_input({"type": "websocket.connect"})
        await browser.receive_output()
        await receive(browser)
        await send(browser, {"type": "subscribe", "worker": "worker-a", "sessionID": "ses_shared"})
        assert (await receive(browser))["type"] == "subscribed"
        for text in ["h", "hello", "hello world"]:
            await send(
                producer,
                {"type": "snapshot", "sessionID": "ses_shared", "messages": [message(text)]},
            )
            frame = await receive(browser)
            assert frame["type"] == "session.live"
            assert frame["messages"][0]["parts"][0]["text"] == text
        assert await sync_to_async(WorkerMessage.objects.count)() == 0

        # A new viewer receives the shared cached overlay without a DB write.
        viewer = browser_connection(live_staff)
        await viewer.send_input({"type": "websocket.connect"})
        await viewer.receive_output()
        await receive(viewer)
        await send(viewer, {"type": "subscribe", "worker": "worker-a", "sessionID": "ses_shared"})
        await receive(viewer)
        cached = await receive(viewer)
        assert cached["sequence"] == frame["sequence"]

        foreign = await open_worker(other_key, "worker-b")
        await receive(foreign)
        await send(
            foreign,
            {"type": "snapshot", "sessionID": "ses_shared", "messages": [message("foreign")]},
        )
        assert await browser.receive_nothing(timeout=0.1)
        assert await viewer.receive_nothing(timeout=0.1)

        # Switching focus unsubscribes the previous group, even with queued frames.
        await send(browser, {"type": "subscribe", "worker": "worker-b", "sessionID": "ses_shared"})
        await receive(browser)
        assert (await receive(browser))["messages"][0]["parts"][0]["text"] == "foreign"
        await send(
            producer,
            {"type": "snapshot", "sessionID": "ses_shared", "messages": [message("final")]},
        )
        await receive(viewer)
        assert await browser.receive_nothing(timeout=0.1)
        await send(producer, {"type": "gap", "sessionID": "ses_shared"})
        assert (await receive(viewer))["type"] == "session.gap"
        assert await sync_to_async(cache.get)(snapshot_key(worker.pk, "ses_shared")) is None
        for socket in [producer, foreign, browser, viewer]:
            await close(socket)

    anyio.run(verify)
    assert WorkerMessage.objects.count() == 0
    response = post_sync(
        api_client,
        sync_payload(
            updates=[
                {"sessionID": "ses_shared", "messages": [message("final")]},
            ]
        ),
    )
    assert response.status_code == 200
    assert response.json()["live"]["protocol_version"] == 1
    assert WorkerMessage.objects.get(session__worker=worker).parts[0]["text"] == "final"
    assert WorkerMessage.objects.count() == 1


@pytest.mark.parametrize(
    "failure", ["invalid-key", "wrong-worker", "revoked", "foreign-session", "oversize"]
)
def test_worker_socket_rejects_invalid_or_revoked_identity_and_oversize(
    live_worker, settings, failure
):
    worker, key = live_worker

    async def verify():
        socket = await open_worker(
            "x" * 40 if failure == "invalid-key" else key,
            "missing" if failure == "wrong-worker" else "worker-a",
        )
        if failure in {"invalid-key", "wrong-worker"}:
            assert (await socket.receive_output())["code"] == 4403
        else:
            await receive(socket)
            if failure == "revoked":
                await sync_to_async(
                    lambda: Worker.objects.filter(pk=worker.pk).update(is_enabled=False)
                )()
                await send(socket, {"type": "ping"})
            elif failure == "foreign-session":
                await send(socket, {"type": "snapshot", "sessionID": "not-owned", "messages": []})
            else:
                settings.WORKER_LIVE_MAX_BYTES = 128
                await send(
                    socket,
                    {
                        "type": "snapshot",
                        "sessionID": "ses_shared",
                        "messages": [message("x" * 200)],
                    },
                )
            assert (await socket.receive_output())["code"] in {4400, 4403}
        await close(socket)

    anyio.run(verify)
    assert WorkerMessage.objects.count() == 0


def test_command_wakeup_is_post_commit_and_does_not_dispatch(live_worker):
    worker, key = live_worker

    def enqueue(rollback=False):
        with transaction.atomic():
            fresh = services.record_worker_meta(
                worker, {"connected": True, "stream_connected": True}
            )
            command, _ = services.enqueue_worker_command(None, "staff", fresh, "create", {})
            if rollback:
                transaction.set_rollback(True)
            return command.pk

    async def verify():
        producer = await open_worker(key)
        await receive(producer)
        pk = await sync_to_async(enqueue)()
        assert (await receive(producer))["type"] == "sync"
        # Notifications may coalesce at the bridge. Socket wakes never dispatch.
        assert await sync_to_async(lambda: worker.commands.get(pk=pk).status)() == "queued"
        while not await producer.receive_nothing(timeout=0.05):
            await receive(producer)
        await sync_to_async(enqueue)(True)
        assert await producer.receive_nothing(timeout=0.1)
        await close(producer)

    anyio.run(verify)


def test_flush_bulk_upserts_once_per_message_and_skips_unchanged_rows(live_worker):
    worker, _ = live_worker
    records = [{"info": {"id": f"msg_{i}", "role": "assistant"}, "parts": []} for i in range(30)]
    with CaptureQueriesContext(connection) as queries:
        services.store_messages(worker, "ses_shared", records + [message("latest")])
    inserts = [
        query for query in queries if 'INSERT INTO "coordinator_workermessage"' in query["sql"]
    ]
    assert len(inserts) == 1
    assert WorkerMessage.objects.count() == 30
    assert WorkerMessage.objects.get(remote_id="msg_1").parts[0]["text"] == "latest"
    with CaptureQueriesContext(connection) as queries:
        services.store_messages(worker, "ses_shared", records + [message("latest")])
    updates = [query for query in queries if 'UPDATE "coordinator_workermessage"' in query["sql"]]
    # Only the out-of-window marking query is needed, no content rewrites.
    assert len(updates) == 1


def test_cache_expiry_does_not_delete_durable_history(live_worker):
    worker, _ = live_worker
    services.store_messages(worker, "ses_shared", [message("durable")])
    key = snapshot_key(worker.pk, "ses_shared")
    cache.set(key, {"messages": []}, timeout=0)
    assert cache.get(key) is None
    assert WorkerMessage.objects.get().parts[0]["text"] == "durable"


def test_live_cache_outage_does_not_break_durable_http_sync(live_worker, api_client, monkeypatch):
    _, key = live_worker

    def unavailable(*args, **kwargs):
        raise ConnectionError("isolated cache outage")

    monkeypatch.setattr(type(caches["default"]), "set", unavailable)

    async def verify():
        socket = await open_worker(key)
        await receive(socket)
        await send(
            socket,
            {"type": "snapshot", "sessionID": "ses_shared", "messages": [message("latest")]},
        )
        assert (await socket.receive_output())["code"] == 1013
        await close(socket)

    anyio.run(verify)
    response = post_sync(
        api_client,
        sync_payload(
            updates=[
                {"sessionID": "ses_shared", "messages": [message("latest")]},
            ]
        ),
    )
    assert response.status_code == 200
    assert WorkerMessage.objects.get().parts[0]["text"] == "latest"
