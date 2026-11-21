"""Built dashboard: Report bubble sizing/responsiveness and PM router option.

Run with `.venv/bin/python scripts/verify_report_bubble_browser.py` after
`npm run build` in frontend/. Uses isolated same-origin API fixtures only.
"""

import functools
import http.server
import re
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
    state = dict(role="owner", replies=[], chat_status=200, chat_calls=0)
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
        elif path == "/api/v1/voice/status":
            body = dict(
                provider="fireworks",
                configured=True,
                speech_provider="elevenlabs",
                speech_configured=False,
                transcription_configured=False,
                tts_configured=False,
                policy_version="x",
                limits=None,
            )
        elif path == "/api/v1/voice/chat":
            state["chat_calls"] += 1
            posts.append(
                dict(pm=request.post_data_json, token=request.headers.get("x-atm-voice-token"))
            )
            status, body = state["chat_status"], state["replies"].pop(0)
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
            re.compile(r"^Page URL: http://127\.0\.0\.1")
        )

        # Mobile portrait and a very short landscape viewport: fits and scrolls.
        for size, label in [
            ((390, 700), "mobile"),
            ((360, 320), "short-mobile"),
            ((700, 300), "landscape"),
        ]:
            page.set_viewport_size(dict(width=size[0], height=size[1]))
            page.get_by_role("radio", name="Tag to PM router").check()
            expect(dialog.get_by_label("Message PM agent")).to_be_visible()
            assert_fits(page, dialog, label)
            if size[1] < 400:
                assert dialog.evaluate("d => d.scrollHeight > d.clientHeight"), label
                dialog.get_by_label("Message PM agent").scroll_into_view_if_needed()
                assert_fits(page, dialog, label + "-scrolled")
            page.get_by_role("radio", name="Standard task form").check()
        page.set_viewport_size(dict(width=1440, height=900))

        # Standard path unchanged: validation, project default, create, refresh.
        expect(dialog.get_by_role("button", name="Create task")).to_be_disabled()
        dialog.get_by_label("Title").fill("Standard report")
        dialog.get_by_role("button", name="Create task").click()
        expect(page.get_by_role("status").filter(has_text="Task #7 created")).to_be_visible()
        assert posts[-1]["project"] == "alpha" and posts[-1]["title"] == "Standard report"

        # PM option is the real conversation: the PM asks, the owner answers, then it creates.
        dialog = open_form(page)
        dialog.get_by_role("radio", name="Tag to PM router").check()
        message = dialog.get_by_label("Message PM agent")
        expect(message).to_have_value(re.compile(r'^Report for project "alpha" from http://127'))
        state["replies"] = [
            dict(text="Which page or feature is affected, and how urgent is it?", events=[]),
            dict(
                text="Created task #9 in alpha as a backlog task.",
                events=[dict(tool="create_task", is_error=False)],
            ),
        ]
        message.fill("The report bubble is tiny")
        dialog.get_by_role("button", name="Send message").click()
        log = dialog.get_by_role("log")
        expect(log).to_contain_text("Which page or feature is affected")
        assert posts[-1]["token"] == "t"
        assert "The report bubble is tiny" in posts[-1]["pm"]["text"]
        message.fill("The dashboard, high priority")
        dialog.get_by_role("button", name="Send message").click()
        expect(log).to_contain_text("create_task · result received")
        expect(log).to_contain_text("Created task #9")
        assert state["chat_calls"] == 2
        assert_fits(page, dialog, "pm-chat")

        # Honest refusal text from the PM is shown as-is; errors are surfaced.
        state["replies"] = [
            dict(
                text="Worker mac is offline, nothing was queued.",
                events=[dict(tool="launch_task", is_error=True)],
            )
        ]
        message.fill("queue it on mac")
        dialog.get_by_role("button", name="Send message").click()
        expect(log).to_contain_text("launch_task · reported an error")
        expect(log).to_contain_text("Worker mac is offline")
        state["chat_status"], state["replies"] = 403, [dict(detail="Owner access required")]
        message.fill("again")
        dialog.get_by_role("button", name="Send message").click()
        expect(dialog.get_by_role("alert")).to_contain_text("Owner access required")
        # Switching back and closing ends the PM conversation (capability revoked).
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        assert revoked == ["/api/v1/voice/sessions/s1"]
        state["chat_status"] = 200

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
