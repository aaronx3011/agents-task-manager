"""Built PM UI: real microphone capture/playback, isolated same-origin API fixtures."""

import functools
import http.server
import io
import json
import math
import struct
import tempfile
import threading
import wave
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
    state = {"role": "owner", "configured": True, "speech": True, "fail": False}
    issued, revoked, turns, errors, clips, speeches, external = [], [], [], [], [], [], []
    long_reply = "Here is a detailed answer. " * 220 + "This is the final sentence."
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\x00\x00" * 24000)

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
                server_time="2026-11-17T00:00:00Z",
                projects=[],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        elif path == "/api/v1/voice/status":
            body = dict(
                provider="fireworks",
                configured=state["configured"],
                speech_provider="elevenlabs",
                speech_configured=state["speech"],
                transcription_configured=state["speech"],
                tts_configured=True,
                policy_version="fixture",
            )
        elif path == "/api/v1/voice/sessions" and request.method == "POST":
            assert request.headers.get("x-csrftoken") == "fixture-csrf"
            issued.append(request.post_data_json)
            route.fulfill(
                status=201,
                json=dict(
                    id=f"00000000-0000-0000-0000-{len(issued):012}",
                    tool_token="short-lived-fixture-only",
                    expires_at="2099-01-01T00:00:00Z",
                ),
            )
            return
        elif path.startswith("/api/v1/voice/sessions/") and request.method == "DELETE":
            revoked.append(path)
            route.fulfill(status=204)
            return
        elif path in {"/api/v1/voice/chat", "/api/v1/voice/transcriptions", "/api/v1/voice/speech"}:
            assert request.headers["x-atm-voice-token"] == "short-lived-fixture-only"
            if path.endswith("/chat"):
                if state["fail"]:
                    route.fulfill(
                        status=502, json={"detail": "Fireworks is temporarily unavailable"}
                    )
                    return
                turns.append(request.post_data_json)
                text = turns[-1]["text"]
                assert set(turns[-1]) == {"text"}  # browser cannot supply system/history/tools
                body = dict(
                    text=long_reply
                    if "detail" in text
                    else "Created task 987 in agents-task-manager."
                    if "Create" in text
                    else "Task 302 is in progress.",
                    events=[
                        dict(tool="create_task" if "Create" in text else "get_task", is_error=False)
                    ],
                )
            elif path.endswith("/transcriptions"):
                clips.append(request.post_data_buffer)
                assert "multipart/form-data" in request.headers["content-type"]
                assert len(clips[-1]) > 200
                assert b"recording.wav" in clips[-1]
                start = clips[-1].index(b"RIFF")
                size = int.from_bytes(clips[-1][start + 4 : start + 8], "little") + 8
                with wave.open(io.BytesIO(clips[-1][start : start + size]), "rb") as clip:
                    assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (
                        1,
                        2,
                        24000,
                    )
                    assert clip.getnframes() > 100
                    assert clip.getnframes() <= 24000 * 16
                    assert any(clip.readframes(clip.getnframes()))
                body = dict(text="Tell me the status of task 302 from voice.")
            else:
                speeches.append(request.post_data_json)
                assert len(speeches[-1]["text"]) <= 2000
                # Playable fixture audio; the provider encoding is not under UI test.
                route.fulfill(content_type="audio/wav", body=audio.getvalue())
                return
        else:
            body = dict(count=0, items=[])
        route.fulfill(json=body)

    with (
        tempfile.TemporaryDirectory(prefix="pm-voice-") as temporary,
        sync_playwright() as playwright,
    ):
        # Deterministic non-silent capture input; Chromium's default fake device
        # can begin with silence. Keep all actual decoded WAV assertions above.
        microphone = Path(temporary) / "microphone.wav"
        with wave.open(str(microphone), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(
                b"".join(
                    struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 48000)))
                    for i in range(48000)
                )
                * 120
            )
        browser = playwright.chromium.launch(
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                f"--use-file-for-fake-audio-capture={microphone}",
                "--autoplay-policy=no-user-gesture-required",
            ]
        )
        context = browser.new_context(permissions=["microphone"])
        context.add_cookies([dict(name="csrftoken", value="fixture-csrf", url=origin)])
        context.route("**/api/**", api)
        context.route(
            "https://api.elevenlabs.io/**", lambda route: external.append(route.request.url)
        )
        context.route(
            "https://api.fireworks.ai/**", lambda route: external.append(route.request.url)
        )
        context.route(
            "https://voice.aaron-henriquez.com/**", lambda route: external.append(route.request.url)
        )
        context.route_web_socket(
            "**/ws/dashboard/**", lambda socket: socket.send(json.dumps(dict(type="connected")))
        )
        # Keep references to real tracks so cleanup can be asserted after Stop.
        context.add_init_script("""
          window.pmTracks = [];
          const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async (options) => {
            if (window.pmMicrophoneGate) await window.pmMicrophoneGate;
            const stream = await original(options);
            window.pmTracks.push(...stream.getTracks());
            return stream;
          };
          window.pmPlayed = 0;
          window.pmRecorderChunks = 0;
          const start = MediaRecorder.prototype.start;
          MediaRecorder.prototype.start = function(...args) {
            window.pmRecorderChunks = 0;
            this.addEventListener('dataavailable', e => {
              if(e.data.size) window.pmRecorderChunks++;
            });
            return start.apply(this, args);
          };
          const play = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function() {
            window.pmPlayed++;
            return play.call(this);
          };
        """)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/?section=voice")
        expect(page.get_by_role("heading", name="PM router", exact=True)).to_be_visible()
        expect(page.get_by_text("Fireworks reasoning · configured", exact=True)).to_be_visible()
        page.get_by_label("Message PM agent").fill("Tell me the status of task 302.")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text("Task 302 is in progress.", exact=True)).to_have_count(1)
        expect(page.get_by_text("get_task · result received", exact=True)).to_have_count(1)
        assert not speeches
        page.get_by_label("Message PM agent").fill("Create a task in agents-task-manager")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text("Created task 987 in agents-task-manager.")).to_be_visible()
        page.get_by_role("button", name="Start voice conversation", exact=True).click()
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Ready for push-to-talk'"
        )
        assert len(issued) == 1 and not revoked  # text-to-voice preserves context
        hold = page.get_by_role("button", name="Push to talk", exact=True)
        hold.hover()
        page.mouse.down()
        expect(hold).to_have_attribute("aria-pressed", "true")
        expect(hold).to_have_attribute("data-capture-ready", "true")
        page.wait_for_function("window.pmRecorderChunks >= 2")
        page.mouse.up()
        expect(
            page.get_by_text("Tell me the status of task 302 from voice.", exact=True)
        ).to_be_visible()
        page.wait_for_function("window.pmPlayed > 0")
        assert clips and speeches
        print("PASS: pointer capture upload and playback", flush=True)
        page.get_by_label("Mute spoken replies").click()
        before = len(clips)
        hold.hover()
        page.mouse.down()
        page.wait_for_timeout(1200)
        page.mouse.move(1, 1)
        page.mouse.up()
        expect(hold).to_have_attribute("aria-pressed", "false")
        page.wait_for_timeout(300)
        assert len(clips) == before
        hold.focus()
        page.keyboard.down("Space")
        expect(hold).to_have_attribute("data-capture-ready", "true")
        page.wait_for_function("window.pmRecorderChunks >= 2")
        page.keyboard.up("Space")
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Ready for push-to-talk'"
        )
        assert len(clips) == before + 1
        print("PASS: keyboard capture", flush=True)
        # Pointer cancellation, capture loss, blur, and hiding cannot send a clip.
        for action in ["pointercancel", "lostpointercapture", "blur", "hidden"]:
            hold.hover()
            page.mouse.down()
            expect(hold).to_have_attribute("aria-pressed", "true")
            if action == "hidden":
                page.evaluate(
                    "Object.defineProperty(document, 'hidden', {configurable:true,value:true}); "
                    "document.dispatchEvent(new Event('visibilitychange')); delete document.hidden"
                )
            elif action == "blur":
                page.get_by_label("Message PM agent").focus()
            else:
                hold.dispatch_event(action)
            page.mouse.up()
            expect(hold).to_have_attribute("aria-pressed", "false")
        page.wait_for_timeout(300)
        assert len(clips) == before + 1
        hold.focus()
        page.keyboard.down("Enter")
        expect(hold).to_have_attribute("aria-pressed", "true")
        page.keyboard.press("Escape")
        page.keyboard.up("Enter")
        expect(hold).to_have_attribute("aria-pressed", "false")
        page.wait_for_timeout(300)
        assert len(clips) == before + 1
        assert hold.evaluate(
            "el => { const e = new MouseEvent('contextmenu', {bubbles:true,cancelable:true}); "
            "el.dispatchEvent(e); return e.defaultPrevented; }"
        )
        assert hold.evaluate("el => getComputedStyle(el).touchAction") == "none"
        assert hold.evaluate("el => getComputedStyle(el).userSelect") == "none"
        page.set_viewport_size(dict(width=390, height=844))
        hold.scroll_into_view_if_needed()
        box = hold.bounding_box()
        touch = context.new_cdp_session(page)
        touch.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": box["x"] + 50, "y": box["y"] + 24}]},
        )
        expect(hold).to_have_attribute("data-capture-ready", "true")
        page.wait_for_function("window.pmRecorderChunks >= 2")
        touch.send(
            "Input.dispatchTouchEvent",
            {"type": "touchMove", "touchPoints": [{"x": box["x"] + 60, "y": box["y"] + 24}]},
        )
        touch.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Ready for push-to-talk'"
        )
        assert len(clips) == before + 2
        print("PASS: mobile touch capture", flush=True)
        assert page.evaluate("getSelection().toString()") == ""
        page.set_viewport_size(dict(width=1280, height=720))
        # A bounded hold must still wait for release: reaching the limit is not
        # permission to send while the owner may still choose to cancel.
        hold.focus()
        page.keyboard.down("Enter")
        expect(hold).to_contain_text("Limit reached", timeout=20000)
        assert len(clips) == before + 2
        page.keyboard.up("Enter")
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Ready for push-to-talk'"
        )
        assert len(clips) == before + 3
        page.get_by_label("Enable spoken replies").click()
        page.get_by_label("Mute spoken replies").click()
        page.get_by_label("Mute microphone").click()
        expect(page.get_by_role("button", name="Unmute microphone", exact=True)).to_be_visible()
        page.get_by_label("Enable spoken replies").click()
        # Long replies pass through every queued chunk, including text after character 4000.
        first_chunk = len(speeches)
        played = page.evaluate("window.pmPlayed")
        page.get_by_label("Message PM agent").fill("Explain in detail")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text(long_reply, exact=True)).to_be_visible()
        page.wait_for_function(
            "(count) => window.pmPlayed > count && "
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Microphone muted'",
            arg=played,
            timeout=90000,
        )
        long_chunks = speeches[first_chunk:]
        assert len(long_chunks) > 4
        assert "".join(chunk["text"] for chunk in long_chunks) == long_reply
        assert page.evaluate("window.pmPlayed") == played + len(long_chunks)
        # Mute stops both the current audio and queued/prefetched future chunks.
        first_chunk = len(speeches)
        played = page.evaluate("window.pmPlayed")
        page.get_by_label("Message PM agent").fill("More detail please")
        page.get_by_label("Send message", exact=True).click()
        page.wait_for_function("(count) => window.pmPlayed > count", arg=played)
        page.get_by_label("Mute spoken replies").click()
        stopped_at = len(speeches)
        page.wait_for_timeout(800)
        assert len(speeches) == stopped_at
        assert 1 <= stopped_at - first_chunk <= 2, (stopped_at, first_chunk, played)
        assert page.evaluate("window.pmPlayed") == played + 1
        page.get_by_role("button", name="End conversation", exact=True).click()
        page.wait_for_function("window.pmTracks.every(track => track.readyState === 'ended')")
        expect(page.get_by_role("button", name="End conversation", exact=True)).to_be_disabled()
        assert revoked
        print("PASS: playback queue, mute and stop", flush=True)
        # Release while permission/device acquisition is pending must never
        # resurrect a recording when the async microphone setup finishes.
        before = len(clips)
        page.evaluate(
            "() => { window.pmMicrophoneGate = "
            "new Promise(resolve => window.pmReleaseMic = resolve); }"
        )
        hold.focus()
        page.keyboard.down("Space")
        expect(hold).to_have_attribute("aria-pressed", "true")
        page.keyboard.up("Space")
        page.evaluate("window.pmReleaseMic(); window.pmMicrophoneGate = undefined")
        expect(hold).to_have_attribute("aria-pressed", "false")
        page.wait_for_function(
            "document.querySelector('.voice-buttons [role=status]').textContent "
            "=== 'Ready for push-to-talk'"
        )
        assert len(clips) == before
        hold.focus()
        page.keyboard.down("Space")
        expect(hold).to_have_attribute("data-capture-ready", "true")
        page.get_by_role("button", name="All tasks 0", exact=True).click()
        page.keyboard.up("Space")
        page.wait_for_function("window.pmTracks.every(track => track.readyState === 'ended')")
        assert len(clips) == before
        page.get_by_role("button", name="Voice agent", exact=True).click()
        page.get_by_role("button", name="Clear conversation", exact=True).click()
        expect(page.get_by_role("heading", name="What would you like to work on?")).to_be_visible()
        page.set_viewport_size(dict(width=390, height=844))
        expect(page.get_by_label("Message PM agent")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        state["speech"] = False
        page.reload()
        expect(
            page.get_by_role("button", name="Start voice conversation", exact=True)
        ).to_be_disabled()
        # Typed fallback works with only Fireworks configured.
        page.get_by_label("Message PM agent").fill("Task status?")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_text("Task 302 is in progress.", exact=True)).to_be_visible()
        state["fail"] = True
        page.get_by_label("Message PM agent").fill("hello")
        page.get_by_label("Send message", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("Fireworks is temporarily unavailable")
        state["role"] = "viewer"
        page.reload()
        expect(page.get_by_text("Owner access required", exact=True)).to_be_visible()
        expect(page.get_by_label("Message PM agent")).to_have_count(0)
        assert not external, external
        assert not errors, errors
        browser.close()
    server.shutdown()
    print(
        "PASS: same-origin PM chat/tools, real microphone STT upload and speech playback, "
        "full long-reply queued playback/prefetch/cancellation, context preservation, "
        "mute/stop/cleanup, typed fallback, errors, mobile and viewer gate; "
        "zero page errors or direct provider requests"
    )


if __name__ == "__main__":
    main()
