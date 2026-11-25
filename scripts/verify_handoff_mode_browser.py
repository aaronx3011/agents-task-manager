"""Built dashboard: the task detail hand-off mode control (#431, task #438).

Covers reading the flag, switching it with a real PATCH body, the viewer and
finished-task locks, and a readable 403. Run with
`uv run --with playwright python scripts/verify_handoff_mode_browser.py` after
`npm run build --prefix frontend`. Uses isolated same-origin API fixtures only.
"""

import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT = dict(slug="alpha", name="Alpha", kind="x", active_count=0, docs_path="")
TASK_ID = 901
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
    models=[dict(providerID="test", modelID="model")],
    agents=[dict(name="build", mode="primary")],
    dropped_events=0,
)


def planning_card(card_id, title, override, project_mode):
    """A board card, with the server-resolved mode the badge reads."""
    return dict(
        id=card_id,
        title=title,
        description="",
        column="Backlog",
        position=card_id,
        labels=[],
        checklist=[],
        instructions="",
        worker="",
        agent="",
        model=None,
        handoff_mode=override,
        effective_handoff_mode=override or project_mode,
        task=None,
        task_status=None,
    )


def planning_body(plan):
    project_mode = plan["handoff_mode"]
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
            handoff_mode=project_mode,
            templates=[],
            columns=["Backlog", "Doing", "Done"],
        ),
        revision=plan["revision"],
        playing=False,
        pause_reason="",
        cards=[
            planning_card(1, "Inherits the project default", plan["cards"][1], project_mode),
            planning_card(2, "Opted out explicitly", plan["cards"][2], project_mode),
        ],
        jobs=[],
    )


def task_body(state):
    """The task detail payload the modal reads, reflecting fixture state."""
    return dict(
        id=TASK_ID,
        title="Hand-off fixture task",
        description="Acceptance criteria",
        project="alpha",
        status=state["status"],
        priority=100,
        claimed_by="alpha",
        claimed_at="2026-11-24T00:00:00Z",
        heartbeat_at="2026-11-24T00:00:00Z",
        depends_on=[],
        blocked_reason="",
        result_summary="",
        handoff_mode=state["mode"],
        is_ready=False,
        dependency_progress=dict(done=0, total=0),
        created_by="alpha",
        created_at="2026-11-24T00:00:00Z",
        updated_at="2026-11-24T00:00:00Z",
        recent_events=[],
        dependencies=[],
    )


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Static handler without per-request logging; the asserts are the output.

    Overriding on the class matters: assigning to the functools.partial that
    ThreadingHTTPServer stores would set an attribute on the partial, not here.
    """

    def log_message(self, *args):
        pass


def main():
    root = Path(__file__).resolve().parents[1] / "frontend/dist"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(QuietHandler, directory=str(root)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    state = dict(role="owner", mode="off", status="in_progress", patch_status=200)
    plan = dict(handoff_mode="off", revision=1, cards={1: "", 2: "off"})
    patches, plan_mutations, errors = [], [], []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        status, body = 200, {}
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
        elif path == f"/api/v1/tasks/{TASK_ID}" and request.method == "PATCH":
            patches.append(request.post_data_json)
            if state["patch_status"] != 200:
                route.fulfill(
                    status=state["patch_status"],
                    json={"detail": "Only the claiming agent may change this task"},
                )
                return
            # Persist like the real API, so a reload must show the new value.
            state["mode"] = request.post_data_json["handoff_mode"]
            body = task_body(state)
        elif path == f"/api/v1/tasks/{TASK_ID}":
            body = task_body(state)
        elif path.endswith(f"/tasks/{TASK_ID}/events"):
            body = dict(items=[], next_before=None)
        elif path.endswith("/projects/alpha/planning"):
            if request.method == "POST":
                sent = request.post_data_json
                plan_mutations.append(sent)
                if sent["operation"] == "settings":
                    plan["handoff_mode"] = sent["settings"]["handoff_mode"]
                if sent["operation"] == "card":
                    card = sent["card"]
                    # Mirror the server: an absent field is simply not written,
                    # which is exactly how the dropped override went unnoticed.
                    if "handoff_mode" in card:
                        plan["cards"][card["id"]] = card["handoff_mode"]
                plan["revision"] += 1
            body = planning_body(plan)
        elif path.endswith("/workers"):
            body = dict(count=1, items=[WORKER])
        else:
            body = dict(count=0, items=[])
        route.fulfill(status=status, json=body)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport=dict(width=1440, height=900))
            context.route("**/api/**", api)
            context.route_web_socket("**/ws/**", lambda socket: None)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def modal():
                page.goto(f"{origin}/?task={TASK_ID}")
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_role("heading", name="Hand-off fixture task")).to_be_visible()
                return dialog

            # 1. An owner reads the current mode without Django admin.
            dialog = modal()
            section = dialog.locator(".handoff-section")
            expect(section).to_be_visible()
            select = dialog.get_by_label("Prompt approvals")
            expect(select).to_have_value("off")
            expect(section.locator(".handoff-badge")).to_have_text("Off")
            expect(section.locator(".handoff-summary")).to_contain_text("waits for a human")
            # Off automates nothing, so no escalation caveat is shown.
            expect(section.locator(".handoff-escalation")).to_have_count(0)
            # Only the two server-side modes are offered; "full_auto" would 400.
            expect(select.locator("option")).to_have_text(["Off", "Semi-automatic"])

            # 2. Switching sends a real PATCH and the UI reflects the new mode.
            select.select_option("semi_auto")
            expect(select).to_have_value("semi_auto")
            assert patches == [{"handoff_mode": "semi_auto"}], patches
            expect(section.locator(".handoff-badge")).to_have_text("Semi-automatic")
            expect(section.locator(".handoff-summary")).to_contain_text("approved automatically")
            # The risky mode must say what still reaches a human.
            escalation = section.locator(".handoff-escalation")
            expect(escalation).to_contain_text("Questions")
            expect(escalation).to_contain_text("deny rule")

            # 3. It survives a reload, because the server persisted it.
            dialog = modal()
            expect(dialog.get_by_label("Prompt approvals")).to_have_value("semi_auto")

            # 4. A 403 surfaces the server's message instead of failing silently.
            state["patch_status"] = 403
            dialog.get_by_label("Prompt approvals").select_option("off")
            expect(dialog.locator(".handoff-section").get_by_role("alert")).to_contain_text(
                "Only the claiming agent may change this task"
            )
            # The rejected write must not be shown as applied.
            assert state["mode"] == "semi_auto", state
            expect(dialog.get_by_label("Prompt approvals")).to_have_value("semi_auto")
            state["patch_status"] = 200

            # 5. A viewer sees the mode but cannot change it.
            state["role"] = "viewer"
            dialog = modal()
            viewer_select = dialog.get_by_label("Prompt approvals")
            expect(viewer_select).to_have_value("semi_auto")
            expect(viewer_select).to_be_disabled()
            expect(dialog.locator(".handoff-section")).to_contain_text("Viewers")
            state["role"] = "owner"

            # 6. A finished task has no session left for the flag to affect.
            state["status"] = "done"
            dialog = modal()
            expect(dialog.get_by_label("Prompt approvals")).to_be_disabled()
            expect(dialog.locator(".handoff-section")).to_contain_text(
                "cannot change on a done task"
            )
            state["status"] = "in_progress"

            # No write escaped either lock.
            assert patches == [
                {"handoff_mode": "semi_auto"},
                {"handoff_mode": "off"},
            ], patches

            # 7. Mobile width keeps the control reachable.
            page.set_viewport_size(dict(width=390, height=844))
            dialog = modal()
            expect(dialog.get_by_label("Prompt approvals")).to_be_visible()
            page.set_viewport_size(dict(width=1440, height=900))

            # --- Planning: setting the mode before a card ever launches (#137) ---

            def planning(tab):
                page.goto(f"{origin}/?section=planning&project=alpha&planning_tab={tab}")
                return page.locator(".planning-page")

            # 8. The project default is settable in planning settings.
            board = planning("settings")
            project_select = board.get_by_label("Project hand-off mode")
            expect(project_select).to_have_value("off")
            expect(project_select.locator("option")).to_have_text(["Off", "Semi-automatic"])
            project_select.select_option("semi_auto")
            page.get_by_role("button", name="Save project settings").click()
            settings_writes = [m for m in plan_mutations if m["operation"] == "settings"]
            assert settings_writes, plan_mutations
            assert settings_writes[-1]["settings"]["handoff_mode"] == "semi_auto"
            assert plan["handoff_mode"] == "semi_auto"

            # 9. The board badges only the cards that will launch unattended.
            board = planning("board")
            inheriting = board.locator(".planning-card", has_text="Inherits the project default")
            opted_out = board.locator(".planning-card", has_text="Opted out explicitly")
            expect(inheriting.locator(".planning-handoff-badge")).to_have_text(
                "Semi-auto approvals"
            )
            # An explicit "off" override must not be badged even though the
            # project default is semi_auto.
            expect(opted_out.locator(".planning-handoff-badge")).to_have_count(0)

            # 10. A card's own override names what it would otherwise inherit.
            inheriting.get_by_role("button", name="Inherits the project default").click()
            card_select = page.get_by_label("Task hand-off mode")
            expect(card_select).to_have_value("")
            expect(card_select.locator("option")).to_have_text(
                ["Project default (Semi-automatic)", "Off", "Semi-automatic"]
            )
            card_select.select_option("off")
            expect(card_select).to_have_value("off")

            # 11. Saving must actually SEND the override. Regression: editableCard
            # whitelisted the card payload and silently dropped handoff_mode, so the
            # choice reverted on reload while the running-task PATCH path worked.
            page.get_by_role("button", name="Save card").click()
            card_writes = [m for m in plan_mutations if m["operation"] == "card"]
            assert card_writes, plan_mutations
            assert card_writes[-1]["card"].get("handoff_mode") == "off", (
                "card save dropped handoff_mode: " + repr(card_writes[-1]["card"])
            )
            assert plan["cards"][1] == "off", plan["cards"]

            # 12. And it must survive a reload, which is what the user sees.
            board = planning("board")
            board.locator(".planning-card", has_text="Inherits the project default").get_by_role(
                "button", name="Inherits the project default"
            ).click()
            expect(page.get_by_label("Task hand-off mode")).to_have_value("off")

            # Turning the project default off un-badges the inheriting card.
            plan["handoff_mode"] = "off"
            board = planning("board")
            expect(
                board.locator(".planning-card", has_text="Inherits the project default").locator(
                    ".planning-handoff-badge"
                )
            ).to_have_count(0)

            context.close()
            browser.close()
    finally:
        server.shutdown()
    assert not errors, errors
    print("hand-off mode browser checks passed")


if __name__ == "__main__":
    main()
