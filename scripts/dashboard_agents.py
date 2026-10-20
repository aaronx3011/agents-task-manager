"""Slowed, repeating real MCP workflows for the isolated browser dashboard demo."""

import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
import django
from asgiref.sync import sync_to_async

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_agent_demo import Metrics, connect  # noqa: E402

ISSUE_URL = "https://github.com/aaronx3011/agents-task-manager/issues/5"
NAMES = ["Atlas", "Aster", "Pixel", "Quill", "Nova", "Orion", "Sage", "Echo"]
PROJECTS = [
    ("demo-platform", "Platform"),
    ("demo-design", "Design system"),
    ("demo-content", "Content"),
]
TITLES = [
    ("Define the API contract", "demo-platform", None),
    ("Implement account endpoints", "demo-platform", 0),
    ("Verify API integration tests", "demo-platform", 1),
    ("Prepare the API release guide", "demo-content", 2),
    ("Design dashboard components", "demo-design", None),
    ("Build the task board layout", "demo-design", 4),
    ("Check responsive breakpoints", "demo-design", 5),
    ("Document the component system", "demo-content", 6),
    ("Review authentication behavior", "demo-platform", None),
    ("Exercise concurrent task claims", "demo-platform", None),
    ("Polish connection indicators", "demo-design", None),
    ("Check keyboard navigation", "demo-design", None),
    ("Write agent onboarding notes", "demo-content", None),
    ("Update deployment instructions", "demo-content", None),
    ("Prepare the changelog", "demo-content", None),
    ("Review project acceptance criteria", "demo-platform", None),
]


def provision():
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from coordinator.models import Agent, Project

    if not settings.COORDINATOR_DEMO:
        raise RuntimeError("This worker is only for the isolated COORDINATOR_DEMO environment")
    admin = get_user_model().objects.create(username="demo", is_staff=True, is_superuser=True)
    admin.set_password(os.environ["DEMO_ADMIN_PASSWORD"])
    admin.save()
    for slug, name in [*PROJECTS, ("demo-control", "Demo coordination")]:
        Project.objects.create(slug=slug, name=name, docs_path=ISSUE_URL)
    keys = {}
    for name in ["Demo coordinator", *NAMES]:
        agent = Agent(name=name)
        keys[name] = agent.set_new_api_key()
        agent.save()
    return keys


async def run():
    keys = await sync_to_async(provision)()
    metrics = Metrics()
    url = os.environ["COORDINATOR_URL"]
    async with AsyncExitStack() as contexts:
        clients = {}
        for name, key in keys.items():
            clients[name] = await contexts.enter_async_context(connect(name, key, url, metrics))
        controller = clients.pop("Demo coordinator")
        await controller.call("get_overview")
        task = await controller.call(
            "create_task",
            title="Coordinate the live browser demo",
            project="demo-control",
            description=ISSUE_URL,
        )
        await controller.call("claim_task", task_id=task["id"])
        print("DEMO READY: demo login is available; eight MCP agents connected", flush=True)

        async def keep_alive():
            while True:
                await anyio.sleep(90)
                await controller.call("heartbeat", task_id=task["id"])

        async def rounds():
            round_number = 0
            while True:
                round_number += 1
                ids = []
                completed = set()
                handed_off = set()
                for title, project, prerequisite in TITLES:
                    created = await controller.call(
                        "create_task",
                        title=f"{title} · batch {round_number}",
                        project=project,
                        description="Demo task: claim, report progress, review and finish via MCP.",
                        depends_on=[] if prerequisite is None else [ids[prerequisite]],
                        priority=50,
                    )
                    ids.append(created["id"])
                print(f"BATCH {round_number}: {len(ids)} tasks ready for the agents", flush=True)

                async def worker(client, completed, ids, handed_off):
                    owned = None
                    try:
                        await client.call("get_overview")
                        while len(completed) < len(ids):
                            claimed = await client.call("claim_next_task")
                            if claimed is None:
                                await anyio.sleep(3)
                                continue
                            owned = claimed["id"]
                            print(f"{client.name}: claimed #{owned} {claimed['title']}", flush=True)
                            await client.call(
                                "add_note",
                                task_id=owned,
                                message="Reading acceptance criteria and checking dependencies",
                            )
                            await anyio.sleep(5)
                            await client.call("heartbeat", task_id=owned)
                            if owned % 7 == 0 and owned not in handed_off:
                                handed_off.add(owned)
                                await client.call(
                                    "release_task",
                                    task_id=owned,
                                    reason="Prepared context; handing off to another agent",
                                )
                                owned = None
                                await anyio.sleep(5)
                                continue
                            if owned % 3 == 0:
                                await client.call(
                                    "update_status",
                                    task_id=owned,
                                    status="blocked",
                                    reason="Waiting for a simulated review response",
                                )
                                await anyio.sleep(8)
                                await client.call(
                                    "update_status", task_id=owned, status="in_progress"
                                )
                            await client.call(
                                "add_note",
                                task_id=owned,
                                message="Implementation prepared; running verification checks",
                            )
                            await anyio.sleep(6)
                            await client.call("heartbeat", task_id=owned)
                            await anyio.sleep(4)
                            await client.call(
                                "update_status",
                                task_id=owned,
                                status="review",
                                result_summary="Work and verification results are ready for review",
                            )
                            await anyio.sleep(7)
                            result = await client.call(
                                "update_status",
                                task_id=owned,
                                status="done",
                                result_summary="Reviewed and completed in the live MCP demo",
                            )
                            completed.add(owned)
                            print(
                                f"{client.name}: completed #{owned} ({result['status']})",
                                flush=True,
                            )
                            owned = None
                            await anyio.sleep(2)
                    finally:
                        if owned is not None:
                            with anyio.CancelScope(shield=True), anyio.move_on_after(10):
                                await client.call(
                                    "release_task",
                                    task_id=owned,
                                    reason="Demo worker stopping; releasing unfinished work",
                                )

                async with anyio.create_task_group() as group:
                    for client in clients.values():
                        group.start_soon(worker, client, completed, ids, handed_off)
                # This is a long-lived visible demo; metrics are not a retained event log.
                metrics.samples.clear()
                print(
                    f"BATCH {round_number}: complete; the next batch starts in 12 seconds",
                    flush=True,
                )
                await anyio.sleep(12)

        async with anyio.create_task_group() as group:
            group.start_soon(keep_alive)
            group.start_soon(rounds)


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    anyio.run(run)
