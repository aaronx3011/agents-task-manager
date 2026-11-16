"""Built ElevenLabs UI + real SDK, with isolated HTTP/WebSocket fixtures."""

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
    state = {"role": "owner", "configured": True, "fail": False}
    issued, revoked, turns, errors, initializations, audio = [], [], [], [], [], []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/v1/auth/me":
            body = dict(id=1, username="pm-owner", role=state["role"], email="")
        elif path.endswith("/auth/csrf"):
            body = {"csrfToken": "fixture-csrf"}
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
        elif path == "/api/v1/voice/status":
            body = dict(
                provider="elevenlabs", configured=state["configured"], policy_version="fixture"
            )
        elif path == "/api/v1/voice/sessions" and request.method == "POST":
            if state["fail"]:
                route.fulfill(status=502, json={"detail": "ElevenLabs is temporarily unavailable"})
                return
            assert request.headers.get("x-csrftoken") == "fixture-csrf"
            issued.append(request.post_data_json)
            body = dict(
                id=f"00000000-0000-0000-0000-{len(issued):012}",
                signed_url="wss://api.elevenlabs.io/v1/convai/conversation?fixture=true",
                tool_token="short-lived-fixture-only",
                expires_at="2099-01-01T00:00:00Z",
            )
            route.fulfill(status=201, json=body)
            return
        elif path.startswith("/api/v1/voice/sessions/") and request.method == "DELETE":
            revoked.append(path)
            route.fulfill(status=204)
            return
        else:
            body = dict(count=0, items=[])
        route.fulfill(json=body)

    def provider(socket):
        def incoming(message):
            event = json.loads(message)
            if event.get("type") == "conversation_initiation_client_data":
                initializations.append(event)
                socket.send(
                    json.dumps(
                        dict(
                            type="conversation_initiation_metadata",
                            conversation_initiation_metadata_event=dict(
                                conversation_id=f"conv_fixture_{len(initializations)}",
                                agent_output_audio_format="pcm_16000",
                                user_input_audio_format="pcm_16000",
                            ),
                        )
                    )
                )
            if event.get("type") == "user_message":
                turns.append(event["text"])
                answer = (
                    "Task 302 is in progress."
                    if "status" in event["text"].lower()
                    else "Created task 987 in agents-task-manager."
                )
                event_id = len(turns)
                tool = dict(
                    type="agent_tool_response",
                    agent_tool_response=dict(
                        tool_name="get_task",
                        tool_call_id=f"tool_{event_id}",
                        tool_type="webhook",
                        is_error=False,
                        is_called=True,
                        event_id=event_id,
                    ),
                )
                socket.send(json.dumps(tool))
                socket.send(json.dumps(tool))  # replay must not duplicate the tool card
                for kind, text in [
                    ("start", ""),
                    ("delta", answer[:10]),
                    ("delta", answer[10:]),
                    ("stop", ""),
                ]:
                    socket.send(
                        json.dumps(
                            dict(
                                type="agent_chat_response_part",
                                text_response_part=dict(type=kind, text=text, event_id=event_id),
                            )
                        )
                    )
                socket.send(
                    json.dumps(
                        dict(
                            type="agent_response",
                            agent_response_event=dict(agent_response=answer, event_id=event_id),
                        )
                    )
                )
            if "user_audio_chunk" in event:
                audio.append(len(event["user_audio_chunk"]))

        socket.on_message(incoming)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ]
        )
        context = browser.new_context(permissions=["microphone"])
        context.add_cookies([dict(name="csrftoken", value="fixture-csrf", url=origin)])
        context.route("**/api/**", api)
        context.route_web_socket("wss://api.elevenlabs.io/**", provider)
        context.route_web_socket(
            "**/ws/dashboard/**", lambda socket: socket.send(json.dumps(dict(type="connected")))
        )
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/?section=voice")
        expect(page.get_by_role("heading", name="PM router", exact=True)).to_be_visible()
        expect(page.get_by_text("ElevenLabs · configured", exact=True)).to_be_visible()
        page.get_by_label("Message PM agent").fill("Tell me the status of task 302.")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text("Task 302 is in progress.", exact=True)).to_have_count(1)
        expect(page.get_by_text("get_task · result received", exact=True)).to_have_count(1)
        assert issued[0]["text_only"] is True
        assert (
            initializations[0]["dynamic_variables"]["secret__atm_voice_token"]
            == "short-lived-fixture-only"
        )
        page.get_by_label("Message PM agent").fill("Create a task in agents-task-manager")
        page.get_by_label("Send message", exact=True).click()
        expect(
            page.get_by_text("Created task 987 in agents-task-manager.", exact=True)
        ).to_have_count(1)
        assert len(issued) == 1  # same conversation, not a new session for every turn
        page.get_by_role("button", name="Start voice conversation", exact=True).click()
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent === 'Listening'"
        )
        assert issued[-1]["text_only"] is False
        assert revoked
        page.wait_for_timeout(500)
        assert audio, "Real SDK did not capture/send microphone audio"
        page.get_by_label("Mute microphone").click()
        expect(page.get_by_role("button", name="Unmute microphone", exact=True)).to_be_visible()
        page.get_by_role("button", name="End conversation", exact=True).click()
        expect(page.get_by_role("button", name="End conversation", exact=True)).to_be_disabled()
        page.get_by_role("button", name="Clear conversation", exact=True).click()
        expect(page.get_by_role("heading", name="What would you like to work on?")).to_be_visible()
        page.set_viewport_size(dict(width=390, height=844))
        expect(page.get_by_label("Message PM agent")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.reload()
        expect(page.get_by_role("heading", name="PM router", exact=True)).to_be_visible()
        state["fail"] = True
        page.get_by_label("Message PM agent").fill("hello")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("ElevenLabs is temporarily unavailable")
        state["role"] = "viewer"
        page.reload()
        expect(page.get_by_text("Owner access required", exact=True)).to_be_visible()
        expect(page.get_by_label("Message PM agent")).to_have_count(0)
        assert not errors, errors
        browser.close()
    server.shutdown()
    print(
        "PASS: real ElevenLabs SDK text/voice protocol, streamed/final dedupe, tool replay, "
        "microphone/mute/stop, session cleanup, errors, mobile and viewer gate; zero page errors"
    )


if __name__ == "__main__":
    main()
