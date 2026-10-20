"""Exercise the real read-only frontend in Chromium against the disposable demo.

Run with `uv run --with playwright python scripts/verify_dashboard_browser.py`.
The extended run seeds audited, already-finished test tasks in the demo container;
--skip-fixtures performs only login, navigation, connection and responsive checks.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def shell(container, code):
    result = subprocess.run(
        ["docker", "exec", container, "python", "manage.py", "shell", "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def seed(container):
    token = secrets.token_hex(4)
    code = (
        "import json\nfrom django.conf import settings\n"
        "from coordinator.models import Project, Task\n"
        "from coordinator.services import admin_save_task, add_note\n"
        "assert settings.COORDINATOR_DEMO, 'Browser fixtures require the isolated demo'\n"
        "project, _ = Project.objects.get_or_create(slug='browser-verification', "
        "defaults={'name':'Browser checks'})\n"
        f"token={token!r}\nids=[]\n"
        "for index in range(45):\n"
        "    title = ('Verified prerequisite' if index == 0 else 'Live task inspection' "
        "if index == 1 else f'Read-only sample {index:02d}') + f' [{token}]'\n"
        "    obj=Task(project=project,title=title,status='done',"
        "result_summary='Verification completed',description='## Acceptance criteria\\n\\n"
        "- Observe real-time updates\\n- Keep the modal open\\n\\n"
        "<script>window.__taskInjected=true</script>')\n"
        "    task=admin_save_task(obj,[],[Task.objects.get(pk=ids[0])] "
        "if index == 1 else [],'Browser verifier')\n"
        "    ids.append(task.pk)\n"
        "for index in range(25):\n"
        "    add_note(None,ids[1],f'Inspection step {index+1}',human_actor='Browser verifier')\n"
        "print(json.dumps({'token':token,'target':ids[1],'prerequisite':ids[0]}))"
    )
    return shell(container, code)


def transition(container, task_id, status, summary):
    return shell(
        container,
        "import json; from django.conf import settings; "
        "from coordinator.services import update_status; assert settings.COORDINATOR_DEMO; "
        f"task=update_status(None,{task_id},{status!r},result_summary={summary!r},"
        "human_actor='Browser verifier'); print(json.dumps({'status':task.status}))",
    )


def capture(page, path, suffix=""):
    if path:
        destination = path.with_name(path.stem + suffix + path.suffix)
        page.screenshot(path=str(destination), full_page=True)


def main():
    expect.set_options(timeout=15000)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--login-url", help="Optional Django origin when testing the Vite proxy")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--container", default="atm-dashboard-demo-web-1")
    parser.add_argument("--skip-fixtures", action="store_true")
    args = parser.parse_args()
    fixture = None if args.skip_fixtures else seed(args.container)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1512, "height": 1050}, reduced_motion="reduce"
        )
        context.add_init_script("""
          window.__sockets = [];
          window.WebSocket = class extends window.WebSocket {
            constructor(...args) { super(...args); window.__sockets.push(this); }
          };
        """)
        page = context.new_page()
        page.set_default_timeout(20000)
        errors, frames = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "websocket",
            lambda socket: socket.on(
                "framereceived", lambda payload: frames.append(json.loads(payload))
            ),
        )
        login_url = args.login_url or args.url
        page.goto(f"{login_url}/admin/login/?next=/dashboard/")
        page.locator("#id_username").fill("demo")
        page.locator("#id_password").fill(os.environ.get("DEMO_ADMIN_PASSWORD", "demo-dashboard"))
        page.locator('input[type="submit"]').click()
        page.wait_for_url("**/dashboard/")
        if login_url != args.url:
            page.goto(f"{args.url}/dashboard/")
        expect(page.get_by_role("status").filter(has_text="Live · WebSocket")).to_be_visible()
        expect(page.get_by_role("button", name="Kanban", exact=True)).to_have_attribute(
            "aria-pressed", "true"
        )
        page.get_by_role("button", name=re.compile("^Open task:")).first.wait_for()
        capture(page, args.screenshot)
        checks = {"staff_login": "passed", "kanban": "passed"}
        if fixture:
            token, target, prerequisite = (
                fixture["token"],
                fixture["target"],
                fixture["prerequisite"],
            )
            title = f"Live task inspection [{token}]"
            page.get_by_label("Project filter").select_option("browser-verification")
            page.get_by_label("Search tasks").fill(token)
            page.wait_for_url(re.compile(f"q={token}"))
            expect(page.get_by_text("45 matching tasks", exact=True)).to_be_visible()
            done = page.get_by_role("region", name="Done tasks", exact=True)
            expect(done.get_by_role("button", name=re.compile("^Open task:"))).to_have_count(20)
            done.get_by_role("button", name=re.compile("Load more")).click()
            expect(done.get_by_role("button", name=re.compile("^Open task:"))).to_have_count(40)
            done.get_by_role("button", name=re.compile("Load more")).click()
            expect(done.get_by_role("button", name=re.compile("^Open task:"))).to_have_count(45)
            page.get_by_role("button", name=f"Open task: {title}", exact=True).click()
            dialog = page.get_by_role("dialog")
            expect(dialog.get_by_role("heading", name=title, exact=True)).to_be_visible()
            assert not page.evaluate("window.__taskInjected === true")
            transition(args.container, prerequisite, "review", "Prerequisite review updated")
            expect(
                dialog.get_by_role("button", name=re.compile("Verified prerequisite")).get_by_text(
                    "In review", exact=True
                )
            ).to_be_visible()
            expect(dialog.get_by_text("0/1", exact=True)).to_be_visible()
            transition(args.container, prerequisite, "done", "Prerequisite verified")
            expect(dialog.get_by_text("1/1", exact=True)).to_be_visible()
            dialog.get_by_role("button", name=re.compile("Verified prerequisite")).click()
            expect(
                dialog.get_by_role("heading", name=f"Verified prerequisite [{token}]", exact=True)
            ).to_be_visible()
            dialog.get_by_role("button", name="Back to previous task").click()
            expect(dialog.get_by_role("heading", name=title, exact=True)).to_be_visible()
            dialog.get_by_role("tab", name="Activity", exact=True).click()
            dialog.get_by_role("button", name="Load earlier activity").click()
            expect(dialog.locator(".timeline-event")).to_have_count(26)
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            page.wait_for_function(
                "id => document.activeElement?.dataset.taskId === String(id)", arg=target
            )
            page.get_by_label("Search tasks").fill(f"#{target}")
            page.get_by_role("button", name="List", exact=True).click()
            page.get_by_label("Status filter").select_option("done")
            expect(
                page.get_by_role("button", name=f"Open task: {title}", exact=True)
            ).to_be_visible()
            assert "project=browser-verification" in page.url
            task_header = page.get_by_role("columnheader", name="Task", exact=True)
            task_header.get_by_role("button").click()
            expect(task_header).to_have_attribute("aria-sort", "ascending")
            task_header.get_by_role("button").click()
            expect(task_header).to_have_attribute("aria-sort", "descending")
            capture(page, args.screenshot, "-list")
            page.get_by_role("button", name=f"Open task: {title}", exact=True).click()
            capture(page, args.screenshot, "-modal")
            other = context.new_page()
            other.on("pageerror", lambda error: errors.append(str(error)))
            other.goto(
                f"{args.url}/dashboard/?project=browser-verification&q=%23{target}&view=kanban"
            )
            expect(
                other.get_by_role("region", name="Done tasks").get_by_role(
                    "button", name=f"Open task: {title}", exact=True
                )
            ).to_be_visible()
            transition(args.container, target, "review", "Live modal update verified")
            expect(dialog.get_by_text("In review", exact=True)).to_be_visible()
            expect(dialog.get_by_text("Live modal update verified", exact=True)).to_be_visible()
            expect(
                other.get_by_role("region", name="In review tasks").get_by_role(
                    "button", name=f"Open task: {title}", exact=True
                )
            ).to_be_visible()
            expect(page.get_by_text("No tasks match your filters", exact=True)).to_be_visible()
            expect(dialog).to_be_visible()
            deep = context.new_page()
            deep.goto(f"{args.url}/dashboard/?view=list&status=done&task={target}")
            expect(
                deep.get_by_role("dialog").get_by_role("heading", name=title, exact=True)
            ).to_be_visible()
            deep.get_by_role("button", name="Close task details").click()
            expect(deep.get_by_role("dialog")).not_to_be_visible()
            # The modal hides before its deferred URL cleanup runs.
            deep.wait_for_function("!new URLSearchParams(window.location.search).get('task')")
            deep.close()
            recovery = context.new_page()
            chunk = re.compile(r"(chunks/TaskModal[^/]*\.js|src/TaskModal\.tsx)")
            recovery.route(chunk, lambda route: route.abort())
            recovery.goto(f"{args.url}/dashboard/?q=%23{target}")
            recovery.get_by_role("button", name=f"Open task: {title}", exact=True).click()
            expect(
                recovery.get_by_role("heading", name="Task details couldn’t load")
            ).to_be_visible()
            expect(recovery.locator("#workspace-title")).to_be_visible()
            recovery.unroute(chunk)
            recovery.get_by_role("button", name="Reload workspace", exact=True).click()
            expect(
                recovery.get_by_role("dialog").get_by_role("heading", name=title, exact=True)
            ).to_be_visible()
            recovery.close()
            other.close()
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog")).not_to_be_visible()
            page.get_by_label("Status filter").select_option("")
            page.get_by_role("button", name="Kanban", exact=True).click()
            checks.update(
                {
                    "lane_load_more": "45 tasks across three pages",
                    "list_filters": "passed",
                    "modal_dependencies": prerequisite,
                    "audit_history": "26 events paged",
                    "focus_restore": "passed",
                    "safe_markdown": "passed",
                    "two_tab_live_update": "passed",
                    "modal_survives_filter_exit": "passed",
                    "direct_link": "passed",
                    "live_prerequisite_updates": "passed",
                    "accessible_sort_state": "passed",
                    "chunk_failure_recovery": "passed",
                }
            )
        else:
            page.get_by_role("button", name="List", exact=True).click()
            expect(page.get_by_role("table")).to_be_visible()
            page.get_by_role("button", name=re.compile("^Open task:")).first.click()
            expect(page.get_by_role("dialog")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog")).not_to_be_visible()
            page.get_by_role("button", name="Kanban", exact=True).click()
        socket_count = page.evaluate("window.__sockets.length")
        page.evaluate("window.__sockets.at(-1).close()")
        page.wait_for_function(
            "n => window.__sockets.length > n && window.__sockets.at(-1).readyState === 1",
            arg=socket_count,
        )
        expect(page.get_by_role("status").filter(has_text="Live · WebSocket")).to_be_visible()
        page.get_by_role("button", name="Switch to light theme").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        capture(page, args.screenshot, "-light")
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        page.get_by_role("button", name="Switch to dark theme").click()
        page.get_by_role("button", name=re.compile(r"^Clear \d")).click()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_label("Board column")).to_be_visible()
        page.get_by_label("Board column").select_option("review" if fixture else "in_progress")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        capture(page, args.screenshot, "-mobile")
        page.get_by_role("button", name=re.compile("^Open task:")).first.click()
        expect(page.get_by_role("dialog")).to_be_visible()
        assert page.get_by_role("dialog").bounding_box()["height"] >= 830
        assert not errors, errors
        checks.update(
            {
                "reconnect": "passed",
                "theme_persistence": "passed",
                "mobile_modal": "passed",
                "javascript_errors": errors,
                "changed_frames": sum(f.get("type") == "changed" for f in frames),
            }
        )
        print(json.dumps(checks))
        browser.close()


if __name__ == "__main__":
    main()
