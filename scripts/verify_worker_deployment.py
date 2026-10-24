"""Disposable PostgreSQL/Redis + multi-ASGI + two real OpenCode endpoint check.

See GitHub #3/#17 for setup. Requires local PostgreSQL CREATEDB credentials,
an explicitly disposable Redis container, Node, and installed OpenCode 1.18.32.
Creates a new isolated database and temporary fixture credentials, then removes
its database/processes/local OpenCode stores on exit. Existing services stay up.
"""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit

import anyio
import httpx
import psycopg
from live_agent_demo import Metrics, connect
from psycopg import sql
from websockets.sync.client import connect as websocket_connect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def wait(label, probe, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (httpx.HTTPError, ValueError, OSError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"Timed out: {label}")


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Process:
    def __init__(self, command, env):
        self.command, self.env = command, env
        self.start()

    def start(self):
        self.child = subprocess.Popen(
            self.command,
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self):
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait(timeout=5)


async def mcp_check(url, key):
    async with connect("worker verifier", key, url, Metrics()) as client:
        await client.call("get_overview")
        task = await client.call("create_task", title="MCP compatibility", project="worker-verify")
        await client.call("claim_task", task_id=task["id"])
        await client.call("heartbeat", task_id=task["id"])
        await client.call("add_note", task_id=task["id"], message="Real stdio MCP verification")
        await client.call(
            "update_status", task_id=task["id"], status="done", result_summary="Verified"
        )


def verify(args, database_url, directory):
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "REDIS_URL": args.redis_url,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_DEBUG": "true",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "DJANGO_SECURE_SSL_REDIRECT": "false",
        "DJANGO_SECURE_COOKIES": "false",
        "COORDINATOR_DEMO": "true",
    }
    os.environ.update(env)
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.test import Client
    from django.utils import timezone

    from coordinator import services
    from coordinator.models import Agent, Project, Task, WorkerCommand, WorkerSession

    call_command("migrate", verbosity=0)
    Project.objects.create(slug="worker-verify", name="Disposable worker verification")
    keys = []
    for suffix in ["a", "b"]:
        agent = Agent(name=f"verify-agent-{suffix}")
        keys.append(agent.set_new_api_key())
        agent.save()
        services.provision_worker(f"verify-worker-{suffix}", agent)
    staff = get_user_model().objects.create_user("verify-staff", is_staff=True)
    login = Client()
    login.force_login(staff)
    cookie = f"{settings.SESSION_COOKIE_NAME}={login.cookies[settings.SESSION_COOKIE_NAME].value}"
    central_port, observer_port = port(), port()
    central = f"http://127.0.0.1:{central_port}"
    observer = f"http://127.0.0.1:{observer_port}"
    processes = []
    paused = False

    def start(command, process_env=env):
        child = Process(command, process_env)
        processes.append(child)
        return child

    def asgi(number, workers):
        return start(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "config.asgi:application",
                "--port",
                str(number),
                "--workers",
                str(workers),
            ]
        )

    def api(path, payload=None, expected=200):
        response = httpx.request(
            "POST" if payload is not None else "GET",
            central + path,
            headers={"Authorization": f"Bearer {keys[0]}"},
            json=payload,
            timeout=10,
        )
        assert response.status_code == expected, (path, response.status_code)
        return response.json()

    def healthy(origin):
        return httpx.get(origin + "/healthz", timeout=2).status_code == 200

    try:
        server = asgi(central_port, 2)
        asgi(observer_port, 1)
        wait("two-worker central ASGI", lambda: healthy(central))
        wait("separate notification ASGI", lambda: healthy(observer))
        endpoints = []
        locals_ = []
        workspace = (directory / "workspace").resolve()
        workspace.mkdir()
        for suffix in ["a", "b"]:
            number = port()
            local_env = {
                **env,
                "XDG_DATA_HOME": str(directory / suffix / "data"),
                "XDG_CONFIG_HOME": str(directory / suffix / "config"),
                "XDG_CACHE_HOME": str(directory / suffix / "cache"),
            }
            local = start(
                [
                    args.opencode,
                    "serve",
                    "--pure",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(number),
                ],
                local_env,
            )
            endpoint = f"http://127.0.0.1:{number}"
            wait(
                "isolated OpenCode " + suffix,
                lambda endpoint=endpoint: (
                    httpx.get(endpoint + "/global/health", timeout=2).json().get("version")
                    == "1.18.32"
                ),
            )
            endpoints.append(endpoint)
            locals_.append(local)
        smoke_env = {
            **env,
            "COORDINATOR_URL": central,
            "WORKER_A_ID": "verify-worker-a",
            "WORKER_A_KEY": keys[0],
            "WORKER_B_ID": "verify-worker-b",
            "WORKER_B_KEY": keys[1],
            "OPENCODE_A_URL": endpoints[0],
            "OPENCODE_B_URL": endpoints[1],
            "SMOKE_DIRECTORY": str(workspace),
        }
        smoke_env.pop("SMOKE_MODEL", None)
        subprocess.run(
            ["node", "bridge/smoke.mjs"], cwd=ROOT, env=smoke_env, check=True, timeout=240
        )
        print(
            "PASS PostgreSQL/Redis + multi-ASGI + two isolated real OpenCode endpoints", flush=True
        )
        target = api("/api/workers/verify-worker-a/sessions")["items"][0]["sessionID"]
        queued = api(
            "/api/commands",
            {
                "worker": "verify-worker-a",
                "action": "prompt",
                "sessionID": target,
                "payload": {
                    "text": "Controlled lost-dispatch fixture; do not execute",
                    "noReply": True,
                },
            },
            202,
        )
        barrier = Barrier(2)

        def concurrent_sync(index):
            barrier.wait(timeout=10)
            return api(
                "/api/worker/sync",
                {
                    "worker": "verify-worker-a",
                    "protocol_version": 1,
                    "meta": {},
                    "cursor": {"batch_id": f"parallel-{index}"},
                },
            )["commands"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(concurrent_sync, range(2)))
        assert [card["id"] for batch in batches for card in batch] == [queued["id"]]
        # Deliberately discard the delivery response without running the command.
        retry = api(
            "/api/worker/sync", {"worker": "verify-worker-a", "protocol_version": 1, "meta": {}}
        )
        assert retry["commands"] == []
        assert api(f"/api/commands/{queued['id']}")["status"] == "dispatched"
        api(
            "/api/worker/sync",
            {
                "worker": "verify-worker-a",
                "protocol_version": 1,
                "meta": {},
                "results": [
                    {
                        "id": queued["id"],
                        "status": "uncertain",
                        "error": "Controlled lost dispatch; not replayed",
                    }
                ],
            },
        )
        assert api(f"/api/commands/{queued['id']}")["deliveryUncertain"]
        print(
            "PASS concurrent ASGI dispatch once; lost dispatch is explicit and never replayed",
            flush=True,
        )
        for index, suffix in enumerate(["a", "b"]):
            start(
                ["node", "bridge/worker.mjs"],
                {
                    **env,
                    "COORDINATOR_URL": central,
                    "COORDINATOR_API_KEY": keys[index],
                    "WORKER_ID": f"verify-worker-{suffix}",
                    "OPENCODE_URL": endpoints[index],
                    "OPENCODE_DIRECTORY": str(workspace),
                    "WORKER_STATE_DIRECTORY": str(directory / f"receipts-{suffix}"),
                    "POLL_INTERVAL_MS": "500",
                },
            )
        wait("bridge reconnected", lambda: api("/api/workers/verify-worker-a")["connected"])
        anyio.run(mcp_check, central, keys[0])
        print("PASS existing ten-tool stdio MCP and task lifecycle", flush=True)

        # Subscribe on a different ASGI process from the one handling mutations.
        ws_url = observer.replace("http://", "ws://") + "/ws/dashboard/"
        with websocket_connect(
            ws_url, additional_headers={"Cookie": cookie}, origin=observer
        ) as websocket:
            assert json.loads(websocket.recv(timeout=10))["type"] == "connected"
            task = api(
                "/api/tasks", {"title": "Heartbeat independence", "project": "worker-verify"}, 201
            )
            api(f"/api/tasks/{task['id']}/claim", {})
            deadline = time.monotonic() + 10
            task_seen = worker_seen = False
            while time.monotonic() < deadline:
                frame = json.loads(websocket.recv(timeout=10))
                if frame.get("task_id") == task["id"]:
                    task_seen = True
                if frame.get("worker_id") and frame.get("subject") in {
                    "workers",
                    "sessions",
                    "messages",
                    "commands",
                }:
                    worker_seen = True
                if task_seen and worker_seen:
                    break
            else:
                raise AssertionError("No cross-process Redis notification")
        print(
            "PASS task/worker WebSocket invalidation across independent ASGI processes", flush=True
        )
        launched = api(f"/api/tasks/{task['id']}/launch", {"worker": "verify-worker-a"}, 201)
        attempt = launched["attempt"]["id"]
        wait(
            "attempt accepted",
            lambda: any(
                row["id"] == attempt and row["outcome"] == "accepted" and row["session"]
                for row in api(f"/api/tasks/{task['id']}")["attempts"]
            ),
        )
        assert api(f"/api/tasks/{task['id']}")["status"] == "in_progress"
        print("PASS task-attempt/session adoption without implicit task completion", flush=True)

        before = api("/api/workers/verify-worker-a/sessions")["count"]
        commands = WorkerCommand.objects.count()
        server.stop()
        time.sleep(1)
        server.start()
        wait("central restart", lambda: healthy(central))
        wait("bridge after central restart", lambda: api("/api/workers/verify-worker-a")["online"])
        assert api("/api/workers/verify-worker-a/sessions")["count"] >= before
        assert WorkerCommand.objects.count() == commands
        print(
            "PASS central restart preserves session/command history without new prompts", flush=True
        )

        locals_[0].stop()
        wait(
            "OpenCode outage observed", lambda: not api("/api/workers/verify-worker-a")["connected"]
        )
        assert WorkerSession.objects.count() >= before
        locals_[0].start()
        wait("OpenCode recovery", lambda: api("/api/workers/verify-worker-a")["connected"])
        print("PASS OpenCode outage/recovery retains central history", flush=True)

        subprocess.run(["docker", "pause", args.redis_container], check=True, capture_output=True)
        paused = True
        started = time.monotonic()
        api(
            f"/api/tasks/{task['id']}/notes",
            {"message": "Redis unavailable; mutation must commit"},
            201,
        )
        assert time.monotonic() - started < 5
        assert (
            Task.objects.get(pk=task["id"])
            .events.filter(message="Redis unavailable; mutation must commit")
            .exists()
        )
        subprocess.run(["docker", "unpause", args.redis_container], check=True, capture_output=True)
        paused = False
        print("PASS paused Redis does not block or roll back committed mutations", flush=True)
        time.sleep(5.2)
        with websocket_connect(
            ws_url, additional_headers={"Cookie": cookie}, origin=observer
        ) as websocket:
            assert json.loads(websocket.recv(timeout=10))["type"] == "connected"
            api(f"/api/tasks/{task['id']}/notes", {"message": "Redis recovered"}, 201)
            wait(
                "Redis recovery event",
                lambda: json.loads(websocket.recv(timeout=10)).get("task_id") == task["id"],
            )
        print("PASS Redis recovery reconnect/refetch notification", flush=True)

        old = timezone.now() - timedelta(minutes=16)
        Task.objects.filter(pk=task["id"]).update(heartbeat_at=old)
        time.sleep(1.2)
        assert Task.objects.get(pk=task["id"]).heartbeat_at == old
        call_command("release_stale_tasks", verbosity=0)
        assert Task.objects.get(pk=task["id"]).status == "pending"
        assert api("/api/workers/verify-worker-a")["online"]
        print(
            "PASS worker sync does not renew task ownership; stale reaper still releases",
            flush=True,
        )
    finally:
        if paused:
            subprocess.run(
                ["docker", "unpause", args.redis_container], check=False, capture_output=True
            )
        for child in reversed(processes):
            child.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("VERIFY_POSTGRES_URL"),
        required=not os.environ.get("VERIFY_POSTGRES_URL"),
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("VERIFY_REDIS_URL"),
        required=not os.environ.get("VERIFY_REDIS_URL"),
    )
    parser.add_argument(
        "--redis-container", required=True, help="Explicitly disposable container to pause"
    )
    parser.add_argument("--opencode", default="opencode")
    args = parser.parse_args()
    name = "coordinator_worker_verify_" + secrets.token_hex(6)
    with psycopg.connect(args.postgres_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with tempfile.TemporaryDirectory(prefix="atm-worker-verify-") as temporary:
                database_url = urlsplit(args.postgres_url)._replace(path=f"/{name}").geturl()
                verify(args, database_url, Path(temporary))
        finally:
            from django.db import connections

            connections.close_all()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
