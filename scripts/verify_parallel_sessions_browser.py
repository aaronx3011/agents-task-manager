"""Production-bundle parallel panel acceptance with deterministic HTTP/WS fixtures.

Real ASGI transport is covered by tests/test_live.py; installed OpenCode calls
are exercised separately by verify_opencode_dashboard.py --parallel-only.
"""

import functools
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1] / "frontend/dist"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    names = ["alpha", "beta", "gamma"]
    models = [{"providerID": "fixture", "modelID": name} for name in ("one", "two")]
    state = {"username": "pin-owner", "role": "owner", "fail": False}
    commands, errors, sockets, subscriptions = [], [], [], []
    pinned, pin_clock = [], [0]
    sequence = 0

    def message(worker, number, text=None):
        return dict(
            id=number,
            remoteID=f"msg_{number}",
            role="assistant",
            content={"time": {"created": number}, "providerID": "fixture", "modelID": "one"},
            parts=[
                {"type": "text", "text": text or f"{worker} saved {number}"},
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "output": f"{worker} tool"},
                },
            ],
            publishedAt="2026-11-19T00:00:00Z",
        )

    def session(worker):
        return dict(
            worker=worker,
            sessionID="shared",
            title=f"Chat {worker}",
            directory=f"/projects/{worker}",
            agentName="build",
            model=models[0],
            executionStatus="idle",
            counts={"messages": 100, "pending": 2},
            window={"count": 100, "complete": False},
            pendingInteractions=[
                {
                    "id": "permission",
                    "sessionID": "shared",
                    "kind": "permission",
                    "title": f"{worker} permission",
                    "replyDispatched": False,
                },
                {
                    "id": "question",
                    "sessionID": "shared",
                    "kind": "question",
                    "replyDispatched": False,
                    "questions": [
                        {
                            "header": f"{worker} question",
                            "text": "Choose",
                            "options": [{"label": "Yes", "description": "Proceed"}],
                            "multiple": False,
                            "custom": False,
                        }
                    ],
                },
            ],
        )

    def worker(name):
        return dict(
            name=name,
            is_enabled=True,
            online=True,
            connected=True,
            stream_connected=True,
            hostname=name,
            opencode_version="1.18.32",
            directory=f"/projects/{name}",
            default_model=models[0],
            models=models,
            agents=[],
            last_error="",
            dropped_events=0,
            counts={"sessions": 1},
        )

    def api(route):
        request = route.request
        path = urlsplit(request.url).path.replace("/api/v1/", "/api/")
        query = parse_qs(urlsplit(request.url).query)
        if path == "/api/auth/me":
            body = dict(id=1, username=state["username"], role=state["role"], email="")
        elif path == "/api/meta":
            body = dict(statuses=[], priorities=[])
        elif path == "/api/dashboard/workspace":
            body = dict(
                server_time="2026-11-19T00:00:00Z",
                projects=[],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        elif path == "/api/workers":
            body = {"count": 3, "items": [worker(name) for name in names]}
        elif path == "/api/commands":
            command = request.post_data_json
            commands.append(command)
            if state["fail"] and command["worker"] == "beta":
                route.fulfill(status=503, json={"detail": "beta delivery unavailable"})
                return
            body = {**command, "id": str(len(commands)), "status": "queued"}
        elif path == "/api/session-pins":
            if request.method == "POST":
                body = request.post_data_json
                pinned[:] = [
                    pin
                    for pin in pinned
                    if (pin["worker"], pin["sessionID"]) != (body["worker"], body["sessionID"])
                ]
                if body["pinned"]:
                    pin_clock[0] += 1
                    title = next(
                        (session(name)["title"] for name in names if name == body["worker"]),
                        body["sessionID"],
                    )
                    pinned.insert(
                        0,
                        {
                            **body,
                            "title": title,
                            "pinned_at": f"2026-11-20T04:{pin_clock[0]:02d}:00Z",
                        },
                    )
                for socket in sockets:
                    socket.send(
                        json.dumps(
                            {
                                "type": "changed",
                                "event_type": "session.pins.changed",
                                "subject": "session_pins",
                            }
                        )
                    )
                body = body
            else:
                body = {"items": pinned}
        elif path == "/api/sessions":
            body = (
                {"count": 0, "items": []}
                if query.get("pending")
                else {"count": 3, "items": [session(name) for name in names]}
            )
        elif path.startswith("/api/workers/"):
            segments = path.split("/")
            name = segments[3]
            if path.endswith("/messages"):
                end = int(query.get("before", ["101"])[0])
                body = {
                    "messages": [message(name, number) for number in range(end - 10, end)],
                    "olderCursor": str(end - 10),
                    "gap": False,
                }
            elif len(segments) == 4:
                body = worker(name)
            elif len(segments) == 5:
                body = {"count": 1, "items": [session(name)]}
            else:
                body = session(name)
        else:
            body = {"count": 0, "items": []}
        route.fulfill(json=body)

    def snapshot(socket, name, text):
        nonlocal sequence
        sequence += 1
        socket.send(
            json.dumps(
                dict(
                    type="session.live",
                    worker=name,
                    sessionID="shared",
                    epoch=f"socket-{len(sockets)}",
                    sequence=sequence,
                    executionStatus="busy",
                    messages=[message(name, 100, text)],
                )
            )
        )

    def connected(socket):
        sockets.append(socket)
        socket.send(json.dumps({"type": "connected"}))

        def receive(raw):
            value = json.loads(raw)
            if value["type"] == "subscribe":
                targets = value["sessions"]
                subscriptions.append(targets)
                assert len(targets) <= 6
                for target in targets:
                    snapshot(
                        socket, target["worker"], f"Live {target['worker']} socket {len(sockets)}"
                    )
            elif value["type"] == "ping":
                socket.send(json.dumps({"type": "pong"}))

        socket.on_message(receive)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1512, "height": 1050})
            context.route("**/api/**", api)
            context.route_web_socket("**/ws/dashboard/", connected)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/?section=sessions")
            page.get_by_role("button", name="Parallel pins", exact=True).click()
            page.get_by_role("button", name="Pin most recently active sessions").click()
            panels = page.locator(".oc-pinned-panel")
            expect(panels).to_have_count(3)
            expect(page.get_by_text("Live · WebSocket", exact=True)).to_be_visible()
            for name in names:
                snapshot(sockets[-1], name, f"Live {name}")
            for name in names:
                panel = page.get_by_label(f"Pinned {name} shared", exact=True)
                expect(panel.locator(".oc-messages")).to_contain_text(f"Live {name}")
                expect(panel.locator(".oc-directory")).to_have_text(f"/projects/{name}")
            for name in names:
                panel = page.get_by_label(f"Pinned {name} shared", exact=True)
                panel.get_by_label("OpenCode model").select_option("fixture/two")
                panel.get_by_label("Message to OpenCode").fill(f"Prompt {name}")
                panel.get_by_role("button", name="Send message", exact=True).click()
                expect(panel.get_by_label("Message to OpenCode")).to_have_value("")
                assert commands[-1]["worker"] == name and commands[-1]["sessionID"] == "shared"
                assert commands[-1]["payload"]["model"] == models[1]
                panel.get_by_role("button", name="Interrupt", exact=True).click()
                panel.get_by_role("button", name="Allow once", exact=True).click()
                panel.get_by_role("radio").check()
                panel.get_by_role("button", name="Submit answers", exact=True).click()
                assert all(
                    command["worker"] == name and command["sessionID"] == "shared"
                    for command in commands[-3:]
                )
            beta = page.get_by_label("Pinned beta shared", exact=True)
            state["fail"] = True
            beta.get_by_label("Message to OpenCode").fill("Retry unchanged")
            beta.get_by_role("button", name="Send message", exact=True).click()
            expect(beta.get_by_role("alert")).to_contain_text("beta delivery unavailable")
            expect(
                page.get_by_label("Pinned alpha shared", exact=True).get_by_role("alert")
            ).to_have_count(0)
            request_id = commands[-1]["client_request_id"]
            state["fail"] = False
            beta.get_by_role("button", name="Send message", exact=True).click()
            expect(beta.get_by_label("Message to OpenCode")).to_have_value("")
            assert commands[-1]["client_request_id"] == request_id
            alpha = page.get_by_label("Pinned alpha shared", exact=True)
            while alpha.locator(".oc-message").count() < 50:
                previous = alpha.locator(".oc-message").count()
                # Pointer auto-scroll also loads history. Invoke the button's
                # native click directly so this bound check requests one page.
                alpha.get_by_role("button", name="Load older messages", exact=True).evaluate(
                    "button => button.click()"
                )
                expect(alpha.locator(".oc-message")).to_have_count(previous + 10)
            expect(alpha.locator(".oc-message")).to_have_count(50)
            expect(
                alpha.get_by_role("button", name="Load older messages", exact=True)
            ).to_have_count(0)
            for number in range(200):
                for name in names:
                    snapshot(sockets[-1], name, f"Burst {name} {number}")
            for name in names:
                panel = page.get_by_label(f"Pinned {name} shared", exact=True)
                expect(panel.locator(".oc-messages")).to_contain_text(f"Burst {name} 199")
                assert panel.locator(".oc-message").count() <= 50
            page.reload()
            expect(panels).to_have_count(3)
            before = len(sockets)
            sockets[-1].close(code=1012, reason="fixture reconnect")
            expect(
                page.get_by_label("Pinned alpha shared", exact=True).locator(".oc-messages")
            ).to_contain_text(f"socket {before + 1}")
            assert {target["worker"] for target in subscriptions[-1]} == set(names)
            page.get_by_label("Live panel limit").select_option("1")
            expect(panels).to_have_count(1)
            page.get_by_label("Live panel limit").select_option("3")
            expect(panels).to_have_count(3)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            for panel in panels.all():
                panel.get_by_label("Message to OpenCode").scroll_into_view_if_needed()
                expect(panel.get_by_label("Message to OpenCode")).to_be_visible()
            # Pins are server state: local storage carries navigation only.
            stored = page.evaluate(
                "JSON.parse(localStorage.getItem('agent-ops-sessions:pin-owner') || '[]')"
            )
            assert stored and all(entry["pinned"] is False for entry in stored)

            # A second client sees the same workspace pins and live updates.
            peer_context = browser.new_context(viewport={"width": 1512, "height": 1050})
            peer_context.route("**/api/**", api)
            peer_context.route_web_socket("**/ws/dashboard/", connected)
            peer = peer_context.new_page()
            peer.on("pageerror", lambda error: errors.append(str(error)))
            peer.goto(origin + "/?section=sessions")
            peer.get_by_role("button", name="Parallel pins", exact=True).click()
            peer_panels = peer.locator(".oc-pinned-panel")
            expect(peer_panels).to_have_count(3)
            page.get_by_label("Pinned gamma shared", exact=True).get_by_role(
                "button", name="Unpin", exact=True
            ).click()
            expect(panels).to_have_count(2)
            expect(peer_panels).to_have_count(2)
            peer.get_by_label("Pin Chat gamma", exact=True).click()
            expect(peer_panels).to_have_count(3)
            expect(panels).to_have_count(3)
            peer_context.close()

            # Viewers read the shared pins but cannot change them.
            state.update(username="other-user", role="viewer")
            page.reload()
            # Panel layout is device-local; pins themselves are shared.
            page.get_by_role("button", name="Parallel pins", exact=True).click()
            expect(panels).to_have_count(3)
            for panel in panels.all():
                expect(panel.get_by_role("button", name="Unpin", exact=True)).to_be_disabled()
                expect(panel.get_by_role("button", name="Interrupt", exact=True)).to_be_disabled()
            state.update(username="pin-owner", role="owner")
            page.reload()
            expect(panels).to_have_count(3)
            page.get_by_label("Pinned alpha shared", exact=True).get_by_role(
                "button", name="Focus session", exact=True
            ).click()
            expect(panels).to_have_count(0)
            expect(page.locator(".oc-conversation h2")).to_have_text("Chat alpha")
            assert not errors, errors
            context.close()
            browser.close()
        print(
            "PASS three concurrent pinned streams, same-ID worker isolation, "
            "models/prompts/interrupts/approvals/questions, scoped errors/retry identity, "
            "reload/reconnect, 50-message cap, 600-frame burst, mobile, "
            "cross-client shared pins without reload, viewer gate and single focus; "
            "zero page errors"
        )
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
