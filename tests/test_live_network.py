"""Actual Node -> Uvicorn -> browser sockets, with delayed HTTP persistence."""

import asyncio
import json
import os
import socket
import threading
from pathlib import Path

import anyio
import pytest
import uvicorn
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import Client
from websockets.asyncio.client import connect

from config.asgi import application
from coordinator import services
from coordinator.models import Agent, Worker, WorkerMessage, WorkerSession

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("adapter", ["packaged", "opencode"])
def test_real_bridge_streams_before_batch_and_flushes_on_completion(
    agent_clients,
    django_user_model,
    tmp_path,
    adapter,
):
    cache.clear()
    services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    staff = Client()
    staff.force_login(django_user_model.objects.create_user("network-human", is_staff=True))
    cookie = staff.cookies.output(header="", sep=";").strip()
    key = agent_clients[0].defaults["HTTP_AUTHORIZATION"].split()[1]
    module = ROOT / "bridge/worker.mjs"
    if adapter == "opencode":
        module = ROOT.parent / "open/opencode_repo/dashboard/src/worker.mjs"
        if not module.exists():
            pytest.skip("Sibling OpenCode checkout is not available")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()

    async def wait_for(probe):
        async with asyncio.timeout(5):
            while not await sync_to_async(services.retry_sqlite(probe))():
                await asyncio.sleep(0.02)

    async def verify():
        await wait_for(lambda: server.started)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "tests/live-worker-fixture.mjs"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "TEST_BRIDGE_MODULE": module.as_uri(),
                "TEST_COORDINATOR_URL": f"http://127.0.0.1:{port}",
                "TEST_WORKER_KEY": key,
                "TEST_WORKER_STATE": str(tmp_path),
            },
        )
        try:
            async with asyncio.timeout(5):
                while b"FIXTURE_READY" not in await process.stdout.readline():
                    if process.returncode is not None:
                        raise AssertionError("Fixture exited before startup")
            await wait_for(lambda: WorkerSession.objects.exists())
            async with connect(
                f"ws://127.0.0.1:{port}/ws/dashboard/",
                origin="http://127.0.0.1",
                additional_headers={"Cookie": cookie},
            ) as browser:
                await browser.recv()
                await browser.send(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "worker": "worker-a",
                            "sessionID": "ses_shared",
                        }
                    )
                )
                async with asyncio.timeout(5):
                    while json.loads(await browser.recv())["type"] != "session.live":
                        pass
                # Allow the socket-ready wakeup's empty baseline batch to settle.
                await asyncio.sleep(0.15)
                process.stdin.write(b'{"text":"streamed before persistence"}\n')
                await process.stdin.drain()
                async with asyncio.timeout(3):
                    while True:
                        frame = json.loads(await browser.recv())
                        if frame["type"] == "session.live" and frame["messages"]:
                            assert (
                                frame["messages"][0]["parts"][0]["text"]
                                == "streamed before persistence"
                            )
                            break
                assert await sync_to_async(WorkerMessage.objects.count)() == 0
                process.stdin.write(b'{"finish":true}\n')
                await process.stdin.drain()
                await wait_for(lambda: WorkerMessage.objects.count() == 1)
                row = await sync_to_async(WorkerMessage.objects.get)()
                assert row.parts[0]["text"] == "streamed before persistence"

                def queue_prompt():
                    worker = Worker.objects.get(name="worker-a")
                    return services.enqueue_worker_command(
                        None,
                        "network-test",
                        worker,
                        "prompt",
                        {"text": "record without a model", "noReply": True},
                        "ses_shared",
                    )[0].pk

                command_id = await sync_to_async(queue_prompt)()
                # A socket wake delivers and acknowledges the command without
                # waiting for the fixture's nine-second background interval.
                await wait_for(
                    lambda: services.WorkerCommand.objects.filter(
                        pk=command_id,
                        status="done",
                    ).exists()
                )
        finally:
            if process.returncode is None:
                process.stdin.write(b'{"stop":true}\n')
                await process.stdin.drain()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                raise AssertionError("Bridge did not shut down") from None
            assert process.returncode == 0, stderr.decode()
        requests = int(stdout.decode().split("FIXTURE_HISTORY_REQUESTS=")[1].split()[0])
        assert requests < 20  # 100 SSE chunks were sampled, not fetched/written individually.
        assert "FIXTURE_PROMPT_CALLS=1" in stdout.decode()

    try:
        anyio.run(verify)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
