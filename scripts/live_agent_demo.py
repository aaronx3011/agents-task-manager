"""Exercise real stdio MCP agents against disposable Docker/PostgreSQL services.

Run: .venv/bin/python scripts/live_agent_demo.py --agents 8 --tasks 40
Requires the built agents-task-manager:local image, Docker, and project dependencies.
Credentials remain in memory. All containers and their temporary data are removed on exit.
"""

import argparse
import json
import math
import os
import secrets
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from statistics import median

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def docker(*args, env=None, check=True):
    return subprocess.run(
        ["docker", *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


class Stack:
    def __init__(self, image):
        suffix = secrets.token_hex(4)
        self.network = f"atm-demo-{suffix}"
        self.db = f"{self.network}-db"
        self.web = f"{self.network}-web"
        self.redis = f"{self.network}-redis"
        self.image = image
        self.containers = []
        self.network_created = False

    def start(self):
        docker("image", "inspect", self.image)
        password = secrets.token_urlsafe(24)
        env = {
            **os.environ,
            "POSTGRES_PASSWORD": password,
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "DJANGO_DEBUG": "false",
            "DATABASE_URL": f"postgresql://coordinator:{password}@{self.db}:5432/coordinator",
            "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DJANGO_SECURE_SSL_REDIRECT": "false",
            "DJANGO_SECURE_COOKIES": "false",
            "REDIS_URL": f"redis://{self.redis}:6379/0",
            "WEB_CONCURRENCY": "2",
        }
        docker("network", "create", self.network)
        self.network_created = True
        docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            self.db,
            "--network",
            self.network,
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=256m",
            "--env",
            "POSTGRES_DB=coordinator",
            "--env",
            "POSTGRES_USER=coordinator",
            "--env",
            "POSTGRES_PASSWORD",
            "postgres:17-alpine",
            env=env,
        )
        self.containers.append(self.db)
        for _ in range(60):
            ready = docker("exec", self.db, "pg_isready", "-U", "coordinator", check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Temporary PostgreSQL did not become ready")
        docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            self.redis,
            "--network",
            self.network,
            "redis:7-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        )
        self.containers.append(self.redis)
        docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            self.web,
            "--network",
            self.network,
            "--publish",
            "127.0.0.1::8000",
            *[
                arg
                for name in [
                    "DJANGO_SECRET_KEY",
                    "DJANGO_DEBUG",
                    "DATABASE_URL",
                    "DJANGO_ALLOWED_HOSTS",
                    "DJANGO_SECURE_SSL_REDIRECT",
                    "DJANGO_SECURE_COOKIES",
                    "REDIS_URL",
                    "WEB_CONCURRENCY",
                ]
                for arg in ["--env", name]
            ],
            self.image,
            env=env,
        )
        self.containers.append(self.web)
        docker("exec", self.web, "python", "manage.py", "migrate", "--noinput")
        address = docker("port", self.web, "8000/tcp").stdout.strip()
        self.url = f"http://{address}"
        for _ in range(60):
            try:
                if httpx.get(f"{self.url}/healthz", timeout=2).status_code == 200:
                    print(
                        f"STACK READY: {self.url} (ASGI 2 workers + PostgreSQL 17 + Redis)",
                        flush=True,
                    )
                    return
            except httpx.RequestError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Temporary Django service did not become healthy")

    def provision(self, count, issue_url):
        names = ["demo-controller"] + [f"demo-agent-{index + 1}" for index in range(count)]
        slugs = ["demo-control", "demo-design", "demo-api", "demo-content", "demo-load"]
        code = (
            "import json\n"
            "from coordinator.models import Agent, Project\n"
            f"for slug in {slugs!r}:\n"
            f"    Project.objects.create(slug=slug, name=slug, docs_path={issue_url!r})\n"
            "keys = {}\n"
            f"for name in {names!r}:\n"
            "    agent = Agent(name=name)\n"
            "    keys[name] = agent.set_new_api_key()\n"
            "    agent.save()\n"
            "print(json.dumps(keys))"
        )
        # This output contains raw keys. Capture it only in memory, never print it.
        output = docker("exec", self.web, "python", "manage.py", "shell", "-c", code).stdout
        return json.loads(output.strip().splitlines()[-1])

    def reap_abandoned(self, task_id):
        code = (
            "from datetime import timedelta; from django.utils import timezone; "
            "from coordinator.models import Task; "
            f"Task.objects.filter(pk={task_id}).update("
            "heartbeat_at=timezone.now()-timedelta(minutes=16))"
        )
        docker("exec", self.web, "python", "manage.py", "shell", "-c", code)
        output = docker("exec", self.web, "python", "manage.py", "release_stale_tasks").stdout
        assert "Released 1 stale task(s)" in output, output

    def audit_counts(self):
        code = (
            "import json; from django.db.models import Count; "
            "from coordinator.models import TaskEvent; "
            "rows=TaskEvent.objects.filter(task__project__slug='demo-load').order_by()"
            ".values('event_type').annotate(n=Count('id')); "
            "print(json.dumps({r['event_type']:r['n'] for r in rows}))"
        )
        output = docker("exec", self.web, "python", "manage.py", "shell", "-c", code).stdout
        return json.loads(output.strip().splitlines()[-1])

    def close(self):
        for name in reversed(self.containers):
            docker("rm", "--force", name, check=False)
        if self.network_created:
            docker("network", "rm", self.network, check=False)
        print(
            "CLEANUP: temporary containers, network, keys and in-memory database removed",
            flush=True,
        )


class Metrics:
    def __init__(self):
        self.phase = "scenarios"
        self.samples = []

    def summary(self, phase):
        samples = [sample for sample in self.samples if sample[0] == phase]
        times = sorted(sample[2] for sample in samples)
        return {
            "calls": len(times),
            "p50_ms": round(median(times), 1),
            "p95_ms": round(times[math.ceil(len(times) * 0.95) - 1], 1),
            "max_ms": round(max(times), 1),
            "tool_errors": sum(sample[3] for sample in samples),
        }


class AgentClient:
    def __init__(self, name, session, metrics):
        self.name, self.session, self.metrics = name, session, metrics

    async def call(self, tool, *, expected_error=None, **arguments):
        started = time.perf_counter()
        result = await self.session.call_tool(tool, arguments)
        elapsed = (time.perf_counter() - started) * 1000
        self.metrics.samples.append((self.metrics.phase, tool, elapsed, bool(result.isError)))
        if result.isError:
            text = " ".join(part.text for part in result.content if part.type == "text")
            if expected_error and expected_error in text:
                print(f"[{self.name}] {tool}: correctly rejected ({expected_error})", flush=True)
                return None
            raise RuntimeError(f"{self.name}/{tool}: {text}")
        assert expected_error is None, f"{tool} should have failed with {expected_error}"
        structured = result.structuredContent
        if structured is not None:
            return structured["result"] if set(structured) == {"result"} else structured
        texts = [part.text for part in result.content if part.type == "text"]
        return json.loads(texts[0]) if texts else None


@asynccontextmanager
async def connect(name, key, url, metrics):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
        cwd=ROOT,
        env={**os.environ, "COORDINATOR_URL": url, "COORDINATOR_API_KEY": key},
    )
    with open(os.devnull, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 11
                yield AgentClient(name, session, metrics)


async def scenarios(alpha, beta, stack, issue_url):
    async def create(client, title, project, **kwargs):
        return await client.call(
            "create_task",
            title=title,
            project=project,
            description=f"Live test acceptance criteria: {issue_url}",
            **kwargs,
        )

    design = await create(alpha, "Approve the API contract", "demo-design")
    implementation = await create(
        alpha, "Implement the approved API", "demo-api", depends_on=[design["id"]], priority=1
    )
    assert (await beta.call("list_tasks", project="demo-api", ready_only=True))["count"] == 0
    await beta.call("claim_task", task_id=implementation["id"], expected_error="HTTP 409")
    await alpha.call("claim_task", task_id=design["id"])
    await beta.call(
        "update_status",
        task_id=design["id"],
        status="done",
        result_summary="Attempted cross-agent update",
        expected_error="HTTP 403",
    )
    await alpha.call("heartbeat", task_id=design["id"])
    await alpha.call("add_note", task_id=design["id"], message="Contract prepared for review")
    await alpha.call(
        "update_status",
        task_id=design["id"],
        status="review",
        result_summary="Contract ready to review",
    )
    assert await beta.call("claim_next_task", project="demo-api") is None
    await alpha.call(
        "update_status", task_id=design["id"], status="done", result_summary="Contract approved"
    )
    claimed = await beta.call("claim_next_task", project="demo-api")
    assert claimed["id"] == implementation["id"]
    await beta.call("heartbeat", task_id=claimed["id"])
    await beta.call("add_note", task_id=claimed["id"], message="Using the approved contract")
    await beta.call(
        "update_status",
        task_id=claimed["id"],
        status="done",
        result_summary="API implemented against approved contract",
    )
    detail = await alpha.call("get_task", task_id=design["id"])
    assert {e["event_type"] for e in detail["recent_events"]} >= {
        "created",
        "claimed",
        "heartbeat",
        "note",
        "status_changed",
    }
    print(
        "PASS: dependency waits through review, unblocks after done; wrong owner rejected",
        flush=True,
    )

    content = await create(alpha, "Write release notes", "demo-content")
    await alpha.call("claim_task", task_id=content["id"])
    await alpha.call(
        "update_status",
        task_id=content["id"],
        status="blocked",
        reason="Waiting for editorial approval",
    )
    await alpha.call("update_status", task_id=content["id"], status="in_progress")
    await alpha.call(
        "update_status",
        task_id=content["id"],
        status="done",
        result_summary="Release notes approved and written",
    )

    handoff = await create(alpha, "Handoff documentation work", "demo-content")
    await alpha.call("claim_task", task_id=handoff["id"])
    await alpha.call("add_note", task_id=handoff["id"], message="Outline ready for the next agent")
    await alpha.call("release_task", task_id=handoff["id"], reason="Handing off a prepared outline")
    await beta.call("claim_task", task_id=handoff["id"])
    await beta.call("heartbeat", task_id=handoff["id"])
    await beta.call(
        "update_status",
        task_id=handoff["id"],
        status="done",
        result_summary="Finished the handed-off documentation",
    )
    print("PASS: blocked/resume and release/handoff across agents", flush=True)

    abandoned = await create(alpha, "Recover abandoned work", "demo-content")
    await alpha.call("claim_task", task_id=abandoned["id"])
    # Advance only this isolated test timestamp instead of waiting fifteen real minutes.
    await anyio.to_thread.run_sync(stack.reap_abandoned, abandoned["id"])
    await alpha.call("heartbeat", task_id=abandoned["id"], expected_error="HTTP 403")
    await beta.call("claim_task", task_id=abandoned["id"])
    await beta.call(
        "update_status",
        task_id=abandoned["id"],
        status="done",
        result_summary="Recovered and completed the expired claim",
    )
    print("PASS: stale reaper recovery; expired owner cannot renew another claim", flush=True)


async def experiment(stack, keys, args):
    metrics = Metrics()
    async with AsyncExitStack() as contexts:
        controller = await contexts.enter_async_context(
            connect(
                "demo-controller",
                keys["demo-controller"],
                stack.url,
                metrics,
            )
        )
        await controller.call("get_overview")
        coordination = await controller.call(
            "create_task",
            title="Run and report live MCP test",
            project="demo-control",
            description=args.issue_url,
        )
        await controller.call("claim_task", task_id=coordination["id"])
        agents = []
        for name, key in keys.items():
            if name != "demo-controller":
                agents.append(
                    await contexts.enter_async_context(connect(name, key, stack.url, metrics))
                )
        print(
            f"CONNECTED: {len(agents)} independent stdio MCP agent sessions + controller",
            flush=True,
        )
        await scenarios(agents[0], agents[1], stack, args.issue_url)
        await controller.call("heartbeat", task_id=coordination["id"])
        for index in range(args.tasks):
            await controller.call(
                "create_task",
                title=f"Parallel work item {index + 1:02d}",
                project="demo-load",
                description="Claim once, heartbeat, add note, finish with an audit trail.",
                priority=10 + index % 5,
            )

        metrics.phase = "concurrent_queue"
        claimed_ids = set()
        completed = dict.fromkeys([agent.name for agent in agents], 0)

        async def worker(agent):
            await agent.call("get_overview")
            while True:
                task = await agent.call("claim_next_task", project="demo-load")
                if task is None:
                    return
                assert task["id"] not in claimed_ids, "DUPLICATE CLAIM"
                claimed_ids.add(task["id"])
                await agent.call("heartbeat", task_id=task["id"])
                await agent.call(
                    "add_note", task_id=task["id"], message=f"{agent.name} verified item"
                )
                await anyio.sleep(0.01)
                result = await agent.call(
                    "update_status",
                    task_id=task["id"],
                    status="done",
                    result_summary=f"{agent.name} completed the live test item",
                )
                assert result["status"] == "done" and result["claimed_by"] is None
                completed[agent.name] += 1
                print(
                    f"[{agent.name}] task #{task['id']}: claim -> heartbeat -> note -> DONE",
                    flush=True,
                )

        started = time.perf_counter()
        async with anyio.create_task_group() as group:
            for agent in agents:
                group.start_soon(worker, agent)
        duration = time.perf_counter() - started
        metrics.phase = "verification"
        assert len(claimed_ids) == args.tasks
        all_tasks = await controller.call("list_tasks", project="demo-load", limit=500)
        assert all_tasks["count"] == args.tasks
        assert all(t["status"] == "done" and t["claimed_by"] is None for t in all_tasks["items"])
        audit = await anyio.to_thread.run_sync(stack.audit_counts)
        assert audit == dict.fromkeys(
            ["created", "claimed", "heartbeat", "note", "status_changed"], args.tasks
        ), audit
        await controller.call(
            "update_status",
            task_id=coordination["id"],
            status="done",
            result_summary=f"Live MCP scenarios and {args.tasks}-task queue verified",
        )
        overview = await controller.call("get_overview")
        assert all(project["counts"]["in_progress"] == 0 for project in overview)
        report = {
            "agents": args.agents,
            "tasks": args.tasks,
            "completed_per_agent": completed,
            "duplicate_claims": 0,
            "unfinished_queue_tasks": 0,
            "queue_elapsed_s": round(duration, 2),
            "queue_tasks_per_minute": round(args.tasks / duration * 60, 2),
            "queue_tool_calls_per_second": round(
                metrics.summary("concurrent_queue")["calls"] / duration,
                2,
            ),
            "queue_mcp_latency": metrics.summary("concurrent_queue"),
            "scenario_mcp_latency": metrics.summary("scenarios"),
            "queue_audit_events": audit,
            "scenario_checks": [
                "dependency readiness",
                "review does not unblock prerequisites",
                "owner enforcement",
                "heartbeats and notes",
                "blocked/resume",
                "release/handoff",
                "stale recovery",
                "expired-owner rejection",
                "concurrent unique claims",
                "complete audit trail",
            ],
        }
        logs = await anyio.to_thread.run_sync(lambda: docker("logs", stack.web))
        assert "Internal Server Error" not in logs.stdout + logs.stderr
        print("\nRESULT_JSON=" + json.dumps(report, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=8)
    parser.add_argument("--tasks", type=int, default=40)
    parser.add_argument("--image", default="agents-task-manager:local")
    parser.add_argument(
        "--issue-url", default="https://github.com/aaronx3011/agents-task-manager/issues/4"
    )
    args = parser.parse_args()
    if not 2 <= args.agents <= 16 or not 1 <= args.tasks <= 500:
        parser.error("Use 2–16 agents and 1–500 tasks")
    stack = Stack(args.image)
    try:
        stack.start()
        keys = stack.provision(args.agents, args.issue_url)
        anyio.run(experiment, stack, keys, args)
    finally:
        stack.close()


if __name__ == "__main__":
    main()
