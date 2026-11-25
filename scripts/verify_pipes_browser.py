"""Built dashboard: pipe creation, progress, checkpoint approval and stop (#437).

Run with `uv run --with playwright python scripts/verify_pipes_browser.py`
after `npm run build --prefix frontend`. Uses isolated same-origin fixtures.
"""

import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT = dict(slug="alpha", name="Alpha", kind="x", active_count=0, docs_path="")
MODEL = dict(providerID="test", modelID="model", name="Test Model")
WORKER = dict(
    name="worker-a",
    agent="alpha",
    is_enabled=True,
    online=True,
    connected=True,
    stream_connected=True,
    hostname="h",
    opencode_version="1",
    directory="/repo",
    create_directory="/repo",
    last_error="",
    last_sync_at="2026-11-24T00:00:00Z",
    default_model=dict(providerID="test", modelID="model"),
    models=[MODEL],
    agents=[dict(name="build", mode="primary")],
    dropped_events=0,
)


def stage(position, name, role, status, artifact="", requires_approval=False):
    return dict(
        id=position,
        position=position,
        name=name,
        role=role,
        status=status,
        worker="worker-a",
        agent="",
        model=dict(providerID="test", modelID="model"),
        directory="/repo",
        requires_approval=requires_approval,
        artifact=artifact,
        attempts=1,
        card=position,
        task=position,
        job=position,
        job_status=status,
        job_detail="",
        session="",
        task_status="done" if status == "completed" else "pending",
    )


def pipe_body(state):
    return dict(
        id=7,
        title="Ship login",
        description="Plan, build and verify",
        status=state["status"],
        detail=state["detail"],
        on_failure="loop",
        max_retries=1,
        retries_used=0,
        approved_stage=state["approved"],
        stages=[
            stage(1, "Plan", "plan", "completed", "PLAN ARTIFACT", True),
            stage(2, "Execute", "execute", "pending"),
            stage(3, "Test", "test", "pending"),
        ],
    )


def planning_body(state):
    return dict(
        project="alpha",
        name="Alpha",
        settings=dict(
            worker="worker-a",
            directory="/repo",
            model=None,
            agent="",
            context="",
            instructions="",
            handoff_mode="off",
            templates=[],
            columns=["Backlog", "Doing", "Done"],
        ),
        revision=state["revision"],
        playing=state["playing"],
        pause_reason=state["pause_reason"],
        cards=[],
        jobs=[],
        pipes=[pipe_body(state)] + state["created"],
    )


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    root = Path(__file__).resolve().parents[1] / "frontend/dist"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(root))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    state = dict(
        role="owner",
        status="awaiting_approval",
        detail="Awaiting approval after stage 1 'Plan'",
        approved=0,
        revision=1,
        playing=False,
        pause_reason="Pipe 'Ship login' awaits approval",
        created=[],
    )
    mutations, errors = [], []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        body = {}
        if path == "/api/v1/auth/me":
            body = dict(id=1, username="o", role=state["role"], email="")
        elif path.endswith("/auth/csrf"):
            body = {"csrfToken": "x"}
        elif path == "/api/v1/meta":
            body = dict(statuses=[], priorities=[])
        elif path == "/api/v1/dashboard/workspace":
            body = dict(
                server_time="2026-11-24T00:00:00Z",
                projects=[PROJECT],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        elif path.endswith("/workers"):
            body = dict(count=1, items=[WORKER])
        elif path.endswith("/projects/alpha/planning"):
            if request.method == "POST":
                sent = request.post_data_json
                mutations.append(sent)
                operation = sent["operation"]
                state["revision"] += 1
                if operation == "pipe_approve":
                    state["status"] = "running"
                    state["approved"] = 1
                    state["detail"] = "Checkpoint after stage 1 approved"
                    state["playing"] = True
                elif operation == "pipe_stop":
                    state["status"] = "failed"
                    state["detail"] = "Pipe stopped by operator"
                    state["playing"] = False
                elif operation == "pipe_create":
                    data = sent["pipe"]
                    state["created"].append(
                        dict(
                            id=8,
                            title=data["title"],
                            description=data.get("description", ""),
                            status="queued",
                            detail="",
                            on_failure=data["on_failure"],
                            max_retries=data["max_retries"],
                            retries_used=0,
                            approved_stage=0,
                            stages=[
                                stage(
                                    index,
                                    item["name"],
                                    item["role"],
                                    "pending",
                                    requires_approval=item["requires_approval"],
                                )
                                for index, item in enumerate(data["stages"], start=1)
                            ],
                        )
                    )
                    state["playing"] = data["play"]
            body = planning_body(state)
        else:
            body = dict(count=0, items=[])
        route.fulfill(status=200, json=body)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport=dict(width=1440, height=900))
            context.route("**/api/**", api)
            context.route_web_socket("**/ws/**", lambda socket: None)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def pipes_page():
                page.goto(f"{origin}/?section=planning&project=alpha&planning_tab=pipes")
                page_view = page.locator(".planning-page")
                expect(page_view.locator(".planning-pipe").first).to_be_visible()
                return page_view

            # 1. Stage progress and artifacts are readable.
            view = pipes_page()
            card = view.locator(".planning-pipe", has_text="Ship login")
            expect(card).to_contain_text("awaiting approval")
            expect(card).to_contain_text("loop on test failure (0/1 retries)")
            stages = card.locator(".planning-pipe-stage")
            expect(stages).to_have_count(3)
            expect(stages.nth(0)).to_contain_text("Plan")
            expect(stages.nth(0)).to_contain_text("completed")
            expect(stages.nth(1)).to_contain_text("Execute")
            expect(stages.nth(2)).to_contain_text("Test")
            expect(stages.nth(0).locator("summary")).to_have_text("Artifact")
            expect(stages.nth(0).locator("pre")).to_contain_text("PLAN ARTIFACT")

            # 2. Approving a checkpoint posts the real pipe_id and updates the UI.
            card.get_by_role("button", name="Approve checkpoint").click()
            expect(card).to_contain_text("running")
            assert mutations[-1] == {"revision": 1, "operation": "pipe_approve", "pipe_id": 7}

            # 3. Create form posts all three stages with worker/model/directory.
            view.get_by_role("button", name="New pipe").click()
            form = view.locator(".planning-pipe-form")
            form.get_by_label("Pipe title").fill("Ship settings")
            form.get_by_label("Shared description / context").fill("Shared context")
            first_stage = form.locator(".planning-pipe-stage-editor").first
            first_stage.locator("select").nth(1).select_option("worker-a")
            first_stage.locator("select").nth(2).select_option("test/model")
            first_stage.get_by_label("Directory").fill("/repo/settings")
            form.get_by_role("button", name="Create pipe").click()
            create = [item for item in mutations if item["operation"] == "pipe_create"][-1]
            stages = create["pipe"]["stages"]
            assert [item["name"] for item in stages] == ["Plan", "Execute", "Test"], stages
            assert [item["role"] for item in stages] == ["plan", "execute", "test"]
            assert stages[0]["worker"] == "worker-a"
            assert stages[0]["model"] == {"providerID": "test", "modelID": "model"}
            assert stages[0]["directory"] == "/repo/settings"
            assert stages[0]["requires_approval"] is True
            assert create["pipe"]["on_failure"] == "stop"
            expect(view).to_contain_text("Ship settings")

            # 4. Stop posts pipe_stop without touching task completion.
            view.locator(".planning-pipe", has_text="Ship settings").get_by_role(
                "button", name="Stop pipe"
            ).click()
            assert mutations[-1]["operation"] == "pipe_stop"

            # 5. A viewer can read but not create, approve or stop.
            state["role"] = "viewer"
            view = pipes_page()
            expect(view.get_by_role("button", name="New pipe")).to_be_disabled()
            expect(view.get_by_role("button", name="Stop pipe").first).to_be_disabled()

            # 6. Mobile keeps the stages and controls reachable.
            state["role"] = "owner"
            page.set_viewport_size(dict(width=390, height=844))
            view = pipes_page()
            expect(view.locator(".planning-pipe").first).to_be_visible()
            expect(view.get_by_role("button", name="New pipe")).to_be_visible()

            context.close()
            browser.close()
    finally:
        server.shutdown()
    assert not errors, errors
    print("pipes browser checks passed")


if __name__ == "__main__":
    main()
