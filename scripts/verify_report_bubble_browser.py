"""Built dashboard: Report bubble sizing/responsiveness and PM router option.

Run with `.venv/bin/python scripts/verify_report_bubble_browser.py` after
`npm run build` in frontend/. Uses isolated same-origin API fixtures only.
"""

import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT = dict(slug="alpha", name="Alpha", kind="x", active_count=0, docs_path="")


def main():
    root = Path(__file__).resolve().parents[1] / "frontend/dist"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)),
    )
    server.RequestHandlerClass.log_message = lambda *args: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    state = dict(role="owner", chat=None, chat_status=200, chat_calls=0)
    posts, revoked, errors = [], [], []

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
                server_time="2026-11-17T00:00:00Z",
                projects=[PROJECT],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        elif path == "/api/v1/tasks" and request.method == "POST":
            posts.append(request.post_data_json)
            status, body = 201, dict(id=7, title=request.post_data_json["title"])
        elif path == "/api/v1/voice/sessions" and request.method == "POST":
            status, body = 201, dict(id="s1", tool_token="t", expires_at="2027-01-01T00:00:00Z")
        elif path.startswith("/api/v1/voice/sessions/") and request.method == "DELETE":
            revoked.append(path)
            route.fulfill(status=204)
            return
        elif path == "/api/v1/voice/chat":
            state["chat_calls"] += 1
            posts.append(
                dict(pm=request.post_data_json, token=request.headers.get("x-atm-voice-token"))
            )
            status, body = state["chat_status"], state["chat"]
        route.fulfill(status=status, json=body)

    def open_form(page):
        page.get_by_role("button", name="Report — create a task").click()
        return page.locator(".report-task-dialog")

    def assert_fits(page, dialog, label):
        box = dialog.bounding_box()
        viewport = page.viewport_size
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"], (label, box)
        assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"], (label, box)
        assert dialog.evaluate("d => d.scrollWidth <= d.clientWidth"), (label, "h-overflow")
        for control in dialog.locator(
            "input:visible, select:visible, textarea:visible, button:visible"
        ).all():
            inner = control.bounding_box()
            assert (
                inner["x"] >= box["x"] - 1
                and inner["x"] + inner["width"] <= box["x"] + box["width"] + 1
            ), (label, inner)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            label,
            "page overflow",
        )
        return box

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=dict(width=1440, height=900))
        context.route("**/api/**", api)
        context.route_web_socket("**/ws/**", lambda socket: None)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/")
        dialog = open_form(page)
        expect(dialog).to_be_visible()

        # Desktop: matches the real (Planning) dialog width, not the old 480px.
        box = assert_fits(page, dialog, "desktop")
        assert box["width"] == 720, box
        assert dialog.locator("textarea").bounding_box()["height"] >= 150
        expect(dialog.get_by_label("Description")).to_have_value(
            __import__("re").compile(r"^Page URL: http://127\.0\.0\.1")
        )

        # Mobile portrait and a very short landscape viewport: fits and scrolls.
        for size, label in [
            ((390, 700), "mobile"),
            ((360, 320), "short-mobile"),
            ((700, 300), "landscape"),
        ]:
            page.set_viewport_size(dict(width=size[0], height=size[1]))
            page.get_by_role("radio", name="Tag to PM router").check()
            page.get_by_label("PM action").select_option("queue")
            assert_fits(page, dialog, label)
            if size[1] < 400:
                assert dialog.evaluate("d => d.scrollHeight > d.clientHeight"), label
                dialog.get_by_role("button", name="Send to PM").scroll_into_view_if_needed()
                assert_fits(page, dialog, label + "-scrolled")
            page.get_by_role("radio", name="Standard task form").check()
        page.set_viewport_size(dict(width=1440, height=900))

        # Standard path unchanged: validation, project default, create, refresh.
        expect(dialog.get_by_role("button", name="Create task")).to_be_disabled()
        dialog.get_by_label("Title").fill("Standard report")
        dialog.get_by_role("button", name="Create task").click()
        expect(page.get_by_role("status").filter(has_text="Task #7 created")).to_be_visible()
        assert posts[-1]["project"] == "alpha" and posts[-1]["title"] == "Standard report"

        # PM option: queued is distinguished from completed.
        dialog = open_form(page)
        dialog.get_by_label("Title").fill("PM queued")
        dialog.get_by_role("radio", name="Tag to PM router").check()
        dialog.get_by_label("PM action").select_option("queue")
        dialog.get_by_label("Worker (optional)").fill("mac")
        state["chat"] = dict(
            text="Queued as job 4.", events=[dict(tool="launch_task", is_error=False)]
        )
        dialog.get_by_role("button", name="Send to PM").click()
        notice = page.get_by_role("status").filter(has_text="Accepted and queued")
        expect(notice).to_contain_text("execution has not completed")
        sent = posts[-1]
        assert (
            sent["token"] == "t"
            and "launch_task" in sent["pm"]["text"]
            and '"worker":"mac"' in sent["pm"]["text"]
        ), sent
        assert revoked == ["/api/v1/voice/sessions/s1"]

        # Honest refusals keep the dialog open with the draft retained.
        cases = [
            (
                "offline",
                dict(
                    text="Worker mac is offline.", events=[dict(tool="launch_task", is_error=True)]
                ),
                "Worker mac is offline",
            ),
            (
                "unknown",
                dict(text="Unknown model.", events=[dict(tool="launch_task", is_error=True)]),
                "Unknown model",
            ),
            (
                "empty",
                dict(text="Queue is empty.", events=[dict(tool="play_queue", is_error=True)]),
                "Queue is empty",
            ),
            ("noop", dict(text="Sure, queued!", events=[]), "did not perform"),
        ]
        dialog = open_form(page)
        dialog.get_by_label("Title").fill("PM refused")
        dialog.get_by_role("radio", name="Tag to PM router").check()
        dialog.get_by_label("PM action").select_option("queue")
        for _name, reply, text in cases:
            state["chat"] = reply
            dialog.get_by_role("button", name="Send to PM").click()
            expect(dialog.get_by_role("alert")).to_contain_text(text)
            expect(dialog.get_by_label("Title")).to_have_value("PM refused")
        # Unauthorized (e.g. session/owner revoked): HTTP error surfaced, nothing success.
        state["chat_status"], state["chat"] = 403, dict(detail="Owner access required")
        dialog.get_by_role("button", name="Send to PM").click()
        expect(dialog.get_by_role("alert")).to_contain_text("Owner access required")
        assert len(revoked) == 1 + len(cases) + 1
        state["chat_status"] = 200

        # Duplicate-submit guard: a slow PM turn sends exactly one chat call.
        calls = state["chat_calls"]
        state["chat"] = dict(text="ok", events=[dict(tool="launch_task", is_error=False)])
        dialog.get_by_role("button", name="Send to PM").dblclick()
        expect(page.get_by_role("status").filter(has_text="Accepted and queued")).to_be_visible()
        assert state["chat_calls"] == calls + 1

        # Escape closes; members never see the PM option.
        dialog = open_form(page)
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        state["role"] = "member"
        page.reload()
        dialog = open_form(page)
        expect(dialog.get_by_role("radio")).to_have_count(0)
        assert_fits(page, dialog, "member")
        browser.close()

    assert not errors, errors
    print("report bubble browser checks passed")


if __name__ == "__main__":
    main()
