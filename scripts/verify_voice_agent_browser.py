"""Check the built voice interface with isolated API and microphone fixtures.

Build frontend first; run using Python with Playwright/Chromium installed.
No real tasks, voice backend, or application database are modified.
"""

import functools
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1] / "frontend/dist"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    state = {"role": "owner", "offline": False, "speech": True}
    requests, errors, sockets = [], [], []

    def api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/v1/auth/me":
            body = dict(id=1, username="voice-owner", role=state["role"], email="")
        elif path == "/api/v1/meta":
            body = dict(statuses=[], priorities=[])
        elif path == "/api/v1/dashboard/workspace":
            body = dict(
                server_time="2026-11-16T00:00:00Z",
                projects=[],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        else:
            body = dict(count=0, items=[])
        route.fulfill(json=body)

    def voice(route):
        path = urlsplit(route.request.url).path
        if state["offline"]:
            route.fulfill(status=502, json={"error": "offline"})
        elif path.endswith("/status"):
            route.fulfill(
                json=dict(
                    speech=state["speech"],
                    ollama=True,
                    modelReady=True,
                    model="qwen3.5:9b",
                    policy={"version": "pm-router-2026-11-16.1", "sha256": "fixture"},
                    mcp={
                        "task-coordinator": {"connected": True},
                        "agent-memory": {"connected": True},
                    },
                )
            )
        elif path.endswith("/chat"):
            requests.append(route.request.post_data_json)
            content = requests[-1]["history"][-1]["content"]
            answer = (
                "Task 296 is in progress."
                if "status" in content.lower()
                else "Created task 987 in agents-task-manager."
            )
            events = [
                dict(type="tool", name="task_coordinator__get_task", result={}),
                dict(type="text", delta=answer[:10]),
                dict(type="text", delta=answer[10:]),
                dict(type="done"),
            ]
            route.fulfill(
                content_type="application/x-ndjson", body="\n".join(map(json.dumps, events))
            )
        elif path.endswith("/voice"):
            route.fulfill(content_type="application/octet-stream", body=bytes(4800))
        else:
            route.fulfill(status=404)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
            ]
        )
        context = browser.new_context(permissions=["microphone"])
        context.route("**/api/**", api)
        context.route("**/voice-agent/api/**", voice)

        def speech(socket):
            sockets.append(socket)
            socket.on_message(
                lambda message: (
                    socket.send(json.dumps(dict(type="session.updated")))
                    if json.loads(message).get("type") == "session.update"
                    else None
                )
            )

        context.route_web_socket("**/voice-agent/api/listen", speech)
        context.route_web_socket(
            "**/ws/dashboard/**", lambda socket: socket.send(json.dumps(dict(type="connected")))
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/?section=voice")
        expect(page.get_by_role("heading", name="Voice agent", exact=True)).to_be_visible()
        expect(page.get_by_text("Tasks · ready", exact=True)).to_be_visible()
        page.get_by_label("Message voice agent").fill("Tell me the status of task 296.")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text("Task 296 is in progress.", exact=True)).to_be_visible()
        assert requests[-1]["history"][-1]["content"] == "Tell me the status of task 296."
        page.get_by_role("button", name="Start microphone", exact=True).click()
        expect(page.get_by_role("button", name="Stop microphone", exact=True)).to_be_visible()
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent === 'Listening'"
        )
        sockets[-1].send(json.dumps(dict(type="input_audio_buffer.speech_started")))
        sockets[-1].send(json.dumps(dict(type="input_audio_buffer.speech_stopped")))
        sockets[-1].send(
            json.dumps(
                dict(
                    type="conversation.item.input_audio_transcription.completed",
                    transcript="Create a task in agents-task-manager called voice fixture.",
                )
            )
        )
        expect(
            page.get_by_text("Created task 987 in agents-task-manager.", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Stop microphone", exact=True).click()
        page.get_by_role("button", name="Clear conversation", exact=True).click()
        expect(page.get_by_role("heading", name="What would you like to work on?")).to_be_visible()
        # Microphone controls must still work after a clear; browser APIs and
        # compiled AudioWorklet are real, transcription is a fixture.
        page.set_viewport_size(dict(width=390, height=844))
        expect(page.get_by_label("Message voice agent")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.reload()
        expect(page.get_by_role("heading", name="Voice agent", exact=True)).to_be_visible()
        state["speech"] = False
        page.reload()
        expect(page.get_by_role("button", name="Start microphone", exact=True)).to_be_disabled()
        expect(page.get_by_label("Message voice agent")).to_be_enabled()
        state["offline"] = True
        page.get_by_role("button", name="Reconnect / check").click()
        expect(page.get_by_role("alert")).to_contain_text("Voice Lab is unavailable")
        state["role"] = "viewer"
        page.reload()
        expect(page.get_by_text("Owner access required", exact=True)).to_be_visible()
        expect(page.get_by_label("Message voice agent")).to_have_count(0)
        assert not errors, errors
        browser.close()
    server.shutdown()
    print(
        "PASS: voice navigation/reload, typed and microphone turns, real AudioWorklet/PCM "
        "playback, service errors, speech-off text fallback, clear, mobile layout, "
        "viewer gate; zero page errors"
    )


if __name__ == "__main__":
    main()
