"""Production-bundle regression with isolated API/WebSocket fixtures.

Build frontend first, then run with a Python environment containing Playwright.
No application database or remote service is modified.
"""

import functools
import http.server
import json
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1] / "coordinator/static/coordinator/app"
    manifest = json.loads((root / "manifest.json").read_text())
    entry = manifest["src/main.tsx"]["file"]
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    statuses = ["pending", "in_progress", "blocked", "review", "done", "cancelled"]
    task = dict(
        id=901,
        title="Synchronized task",
        description="Fixture work",
        project="fixture",
        status="in_progress",
        priority=100,
        claimed_by="fixture-agent",
        claimed_at=None,
        heartbeat_at=None,
        depends_on=[],
        dependency_progress={"done": 0, "total": 0},
        blocked_reason="",
        result_summary="",
        is_ready=False,
        created_by="fixture-agent",
        created_at="2026-11-10T00:00:00Z",
        updated_at="2026-11-10T00:00:00Z",
    )
    state = {"reject": False}
    writes, errors, sockets = [], [], []
    project = dict(slug="fixture", name="Fixture", kind="code", active_count=1, docs_path="")

    def notify():
        for socket in sockets:
            socket.send(json.dumps(dict(type="changed", task_id=901, event_type="status_changed")))

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        if request.method == "POST" and path.startswith("/api/tasks/901/"):
            writes.append((path, request.post_data_json))
            if state["reject"]:
                route.fulfill(status=409, json={"detail": "Concurrent lifecycle conflict"})
                return
            values = request.post_data_json
            task["status"] = "pending" if path.endswith("/release") else values["status"]
            task["result_summary"] = values["result_summary"]
            task["blocked_reason"] = values["reason"] if task["status"] == "blocked" else ""
            response = task
            # Deliberately no notification: local mutation invalidation must work.
        elif path == "/api/tasks":
            match = not query.get("status") or query["status"][0] == task["status"]
            response = {"count": int(match), "items": [task] if match else []}
        elif path == "/api/projects/fixture/planning":
            response = dict(
                project="fixture",
                name="Fixture",
                revision=1,
                playing=False,
                pause_reason="Queue paused",
                settings=dict(
                    worker="",
                    directory="",
                    model=None,
                    agent="",
                    context="",
                    instructions="",
                    templates=[],
                    columns=["Backlog", "Done"],
                ),
                cards=[
                    dict(
                        id=1,
                        title=task["title"],
                        description="",
                        column="Backlog",
                        position=0,
                        labels=[],
                        checklist=[],
                        instructions="",
                        agent="",
                        model=None,
                        task=901,
                        task_status=task["status"],
                    )
                ],
                jobs=[],
            )
        elif "workspace" in path:
            counts = {status: int(task["status"] == status) for status in statuses}
            response = dict(
                server_time="2026-11-10T00:00:00Z",
                counts=counts,
                total=1,
                ready_count=0,
                projects=[project],
                agents=[],
                events=[],
            )
        else:
            response = {"count": 0, "items": []}
        route.fulfill(json=response)

    def websocket(socket):
        sockets.append(socket)
        socket.send(json.dumps({"type": "connected"}))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("CHROMIUM_EXECUTABLE")
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("**/api/**", api)
            context.route("**/dashboard/workspace/**", api)
            context.route(
                "**/static/coordinator/app/**",
                lambda route: route.fulfill(
                    path=str(
                        root
                        / urlsplit(route.request.url).path.split("/static/coordinator/app/", 1)[1]
                    )
                ),
            )
            context.route(
                "**/dashboard/?*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body=f'<div id="root" data-username="Fixture"></div>'
                    f'<script type="module" src="/{entry}"></script>',
                ),
            )
            context.route_web_socket("**/ws/dashboard/", websocket)
            board = context.new_page()
            board.on("pageerror", lambda error: errors.append(str(error)))
            board.goto(origin + "/dashboard/?view=kanban")

            def lane(name):
                return board.get_by_role("region", name=f"{name} tasks", exact=True)

            expect(
                lane("In progress").get_by_role("button", name="Open task: Synchronized task")
            ).to_be_visible()

            planning = context.new_page()
            planning.on("pageerror", lambda error: errors.append(str(error)))
            planning.goto(
                origin + "/dashboard/?section=planning&project=fixture&planning_tab=board"
            )
            planning.get_by_text("Execution status: In progress", exact=True).click()
            select = planning.get_by_label("Execution status for task 901")
            select.select_option("done")
            submit = planning.get_by_role("button", name="Update task status", exact=True)
            expect(submit).to_be_disabled()
            planning.get_by_label("Outcome / handoff").fill("Verified outcome")
            submit.click()
            expect(planning.get_by_text("Execution status: Done", exact=True)).to_be_visible()
            assert writes[-1][1]["result_summary"] == "Verified outcome"
            # Recovery works across tabs even without a WebSocket notice.
            expect(
                lane("Done").get_by_role("button", name="Open task: Synchronized task")
            ).to_be_visible(timeout=9000)
            expect(lane("In progress").locator(".task-card")).to_have_count(0)

            # A real notification updates the board before its periodic fallback.
            task["status"] = "review"
            notify()
            expect(lane("In review").locator(".task-card")).to_have_count(1, timeout=4000)
            expect(lane("Done").locator(".task-card")).to_have_count(0)
            expect(planning.get_by_text("Execution status: In review", exact=True)).to_be_visible()

            # Blocker validation, payload, and server conflict visibility.
            select.select_option("blocked")
            expect(submit).to_be_disabled()
            planning.get_by_label("Blocker reason").fill("Waiting for owner")
            state["reject"] = True
            submit.click()
            expect(planning.get_by_role("alert")).to_have_text("Concurrent lifecycle conflict")
            assert task["status"] == "review"
            state["reject"] = False
            submit.click()
            expect(planning.get_by_text("Execution status: Blocked", exact=True)).to_be_visible()
            assert writes[-1][1]["reason"] == "Waiting for owner"

            # Release uses the dedicated lifecycle endpoint, then navigation to
            # All Tasks must not restore stale cached in-progress/done lanes.
            select.select_option("pending")
            submit.click()
            expect(planning.get_by_text("Execution status: Pending", exact=True)).to_be_visible()
            assert writes[-1][0].endswith("/release")
            planning.get_by_role("button", name="All tasks", exact=False).first.click()
            expect(
                planning.get_by_role("region", name="Pending tasks", exact=True).locator(
                    ".task-card"
                )
            ).to_have_count(1)

            # Filtered list also reconciles an external completion without notice.
            board.goto(origin + "/dashboard/?view=list&status=blocked")
            expect(board.get_by_text("No matching tasks", exact=True)).to_be_visible()
            task["status"] = "blocked"
            expect(board.locator(".table-task")).to_have_count(1, timeout=9000)
            task["status"] = "done"
            expect(board.locator(".table-task")).to_have_count(0, timeout=9000)

            planning.set_viewport_size({"width": 390, "height": 844})
            planning.goto(origin + "/dashboard/?section=planning&project=fixture")
            planning.get_by_text("Execution status: Done", exact=True).click()
            expect(planning.get_by_label("Execution status for task 901")).to_be_visible()
            assert not errors, errors
            browser.close()
            print(
                "PASS: cross-tab recovery, WebSocket transitions, lane removal, status validation, "
                "conflict/retry, release, navigation, filtered list and mobile; no page errors"
            )
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
