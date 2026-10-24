"""Browser verification of the unified coordinator UI with disposable workers.

Uses a temporary SQLite database, staff/agent fixtures, a real isolated installed
OpenCode endpoint/bridge and a controlled interaction-publishing worker. No model
calls, public credentials or existing databases/services are changed.
Run: uv run --with playwright python scripts/verify_opencode_dashboard.py
"""

import argparse
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_health(url, child):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Fixture service exited before becoming healthy")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Fixture service did not become healthy")


def check_module_graph(scripts, errors):
    entry_urls = {url for url in scripts if re.search(r"/app(?:[-.][^/]+)?\.js$", url)}
    assert len(entry_urls) == 1, entry_urls
    assert re.search(r"/app-[\w-]+\.js$", next(iter(entry_urls))), entry_urls
    assert not any(url.endswith("/app.js") for url in scripts), scripts
    assert not errors, errors


def run(directory, screenshots, assets_only=False):
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{directory / 'dashboard.sqlite3'}",
        "REDIS_URL": "",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_DEBUG": "false",
        "STATIC_ROOT": str(directory / "staticfiles"),
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "DJANGO_SECURE_SSL_REDIRECT": "false",
        "DJANGO_SECURE_COOKIES": "false",
        "COORDINATOR_DEMO": "true",
    }
    for name in ("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"):
        env.pop(name, None)
    os.environ.update(env)
    sys.path.insert(0, str(ROOT))
    import django

    django.setup()
    import httpx
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from playwright.sync_api import expect, sync_playwright

    from coordinator import services
    from coordinator.models import Agent, Project

    call_command("migrate", verbosity=0)
    call_command("collectstatic", verbosity=0, interactive=False)
    password = secrets.token_urlsafe(24)
    get_user_model().objects.create_user("dashboard-verifier", password=password, is_staff=True)
    project = Project.objects.create(slug="browser-check", name="Browser checks")
    services.create_task(None, project, "Existing task UI remains available")
    keys = {}
    for name in ("real-opencode", "interaction-worker", "offline-worker"):
        agent = Agent(name=name)
        keys[name] = agent.set_new_api_key()
        agent.save()
        services.provision_worker(name, agent)
    central_port, local_port = port(), port()
    origin = f"http://127.0.0.1:{central_port}"
    local = f"http://127.0.0.1:{local_port}"
    workspace = directory / "workspace"
    workspace.mkdir()
    processes = []
    stop = threading.Event()
    interaction_results = []
    interaction_errors = []
    browser_errors = []
    text = "Live fixture message"
    injected = "\n<script>window.workerInjected=true</script>"

    def publish_interactions():
        pending = {
            "complete": True,
            "permissions": [
                {
                    "id": "per_test",
                    "sessionID": "ses_control",
                    "title": "Controlled shell approval",
                    "patterns": ["fixture command"],
                }
            ],
            "questions": [
                {
                    "id": "que_test",
                    "sessionID": "ses_control",
                    "questions": [
                        {
                            "header": "Color",
                            "question": "Choose a color",
                            "options": [{"label": "Blue"}, {"label": "Green"}],
                            "custom": True,
                        },
                        {
                            "header": "Features",
                            "question": "Choose several",
                            "options": [{"label": "Logs"}, {"label": "Metrics"}],
                            "multiple": True,
                            "custom": False,
                        },
                    ],
                }
            ],
        }
        results = []
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + keys["interaction-worker"]},
            timeout=15,
        ) as client:
            while not stop.is_set():
                try:
                    response = client.post(
                        "/api/worker/sync",
                        json={
                            "worker": "interaction-worker",
                            "protocol_version": 1,
                            "meta": {
                                "connected": True,
                                "stream_connected": True,
                                "hostname": "fixture",
                                "opencode_version": "1.18.32",
                            },
                            "sessions": [
                                {
                                    "id": "ses_control",
                                    "title": "Controlled interactions",
                                    "directory": str(workspace),
                                    "time": {"updated": 1},
                                }
                            ],
                            "updates": [
                                {
                                    "sessionID": "ses_control",
                                    "messages": [
                                        {
                                            "info": {
                                                "id": "msg_control",
                                                "role": "assistant",
                                                "time": {"created": 1},
                                            },
                                            "parts": [
                                                {
                                                    "type": "text",
                                                    "text": text + injected,
                                                },
                                                {"type": "reasoning", "text": "Reasoning fixture"},
                                                {
                                                    "type": "tool",
                                                    "tool": "bash",
                                                    "state": {
                                                        "status": "completed",
                                                        "input": {"command": "fixture"},
                                                        "output": "Safe tool output",
                                                    },
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ],
                            "pending": pending,
                            "results": results,
                        },
                    )
                    response.raise_for_status()
                    results = []
                    for command in response.json()["commands"]:
                        interaction_results.append(command)
                        if command["action"] == "permission.reply":
                            pending["permissions"] = []
                        elif command["action"] in ("question.reply", "question.reject"):
                            pending["questions"] = []
                        results.append({"id": command["id"], "status": "done", "result": None})
                except httpx.HTTPError as error:
                    if not stop.is_set():
                        interaction_errors.append(type(error).__name__)
                stop.wait(0.4)

    publisher = None
    with (directory / "services.log").open("w+") as log:

        def start(command, process_env=env):
            child = subprocess.Popen(command, cwd=ROOT, env=process_env, stdout=log, stderr=log)
            processes.append(child)
            return child

        try:
            central = start(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "config.asgi:application",
                    "--port",
                    str(central_port),
                ]
            )
            wait_health(origin + "/healthz", central)
            if not assets_only:
                opencode = start(
                    [
                        "opencode",
                        "serve",
                        "--pure",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        str(local_port),
                    ],
                    # Isolate OpenCode only. Changing the verifier's own XDG
                    # cache hides Playwright's installed browsers on Linux.
                    {
                        **env,
                        "XDG_DATA_HOME": str(directory / "data"),
                        "XDG_CONFIG_HOME": str(directory / "config"),
                        "XDG_CACHE_HOME": str(directory / "cache"),
                    },
                )
                wait_health(local + "/global/health", opencode)
                start(
                    ["node", "bridge/worker.mjs"],
                    {
                        **env,
                        "COORDINATOR_URL": origin,
                        "COORDINATOR_API_KEY": keys["real-opencode"],
                        "WORKER_ID": "real-opencode",
                        "OPENCODE_URL": local,
                        "OPENCODE_DIRECTORY": str(workspace),
                        "WORKER_STATE_DIRECTORY": str(directory / "receipts"),
                        "POLL_INTERVAL_MS": "500",
                    },
                )
                publisher = threading.Thread(target=publish_interactions, daemon=True)
                publisher.start()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(
                    viewport={"width": 1512, "height": 1050}, reduced_motion="reduce"
                )
                page = context.new_page()
                page.set_default_timeout(25000)
                errors, frames, scripts = browser_errors, [], []
                page.on(
                    "request",
                    lambda request: (
                        scripts.append(request.url) if request.resource_type == "script" else None
                    ),
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "websocket",
                    lambda socket: socket.on(
                        "framereceived", lambda value: frames.append(json.loads(value))
                    ),
                )
                page.goto(origin + "/admin/login/?next=/dashboard/")
                page.locator("#id_username").fill("dashboard-verifier")
                page.locator("#id_password").fill(password)
                page.locator('input[type="submit"]').click()
                page.wait_for_url("**/dashboard/")
                expect(page.get_by_role("button", name="Kanban", exact=True)).to_be_visible()
                expect(
                    page.get_by_text("Existing task UI remains available", exact=True)
                ).to_be_visible()
                if assets_only:
                    page.goto(origin + "/dashboard/?section=opencodes")
                    expect(page.get_by_role("heading", name="Connected OpenCodes.")).to_be_visible()
                    expect(page.locator(".oc-worker")).to_have_count(3)
                    page.reload()
                    expect(page.locator(".oc-worker")).to_have_count(3)
                    page.get_by_role("button", name="All tasks", exact=False).click()
                    page.get_by_role(
                        "button", name="Open task: Existing task UI remains available", exact=True
                    ).click()
                    expect(page.get_by_role("dialog")).to_be_visible()
                    page.keyboard.press("Escape")
                    check_module_graph(scripts, errors)
                    context.close()
                    browser.close()
                    print(
                        "PASS production-collected lazy dashboard/task modules and deep reload; "
                        "one entry URL and zero browser errors"
                    )
                    return
                page.get_by_role("button", name="OpenCodes", exact=True).click()
                page.locator(".oc-worker").filter(has_text="real-opencode").click()
                expect(
                    page.locator(".oc-worker-header").get_by_text("Connected", exact=True)
                ).to_be_visible()
                page.get_by_role("button", name="New session", exact=True).click()
                page.get_by_label("New session title").fill("Unified dashboard live smoke")
                page.get_by_role("button", name="Create session", exact=True).click()
                expect(
                    page.locator(".oc-conversation").get_by_role(
                        "heading", name="Unified dashboard live smoke", exact=True
                    )
                ).to_be_visible()
                page.get_by_label("Record only (no model reply)").check()
                page.get_by_label("Message to OpenCode").fill("Browser to Django to real OpenCode")
                page.get_by_role("button", name="Send message", exact=True).click()
                expect(
                    page.locator(".oc-message").get_by_text(
                        "Browser to Django to real OpenCode", exact=True
                    )
                ).to_be_visible()
                page.get_by_role("button", name="Interrupt", exact=True).click()
                expect(
                    page.locator(".oc-commands").get_by_text("interrupt", exact=False).first
                ).to_be_visible()
                page.locator(".oc-worker").filter(has_text="offline-worker").click()
                expect(page.get_by_role("button", name="New session", exact=True)).to_be_disabled()
                page.locator(".oc-worker").filter(has_text="interaction-worker").click()
                expect(page.get_by_role("form", name="Pending questions")).to_be_visible()
                page.get_by_label("Custom answer 1").fill("Custom blue")
                page.get_by_label("Logs", exact=True).check()
                page.get_by_label("Metrics", exact=True).check()
                page.locator(".oc-worker").filter(has_text="real-opencode").click()
                page.locator(".oc-worker").filter(has_text="interaction-worker").click()
                expect(page.get_by_label("Custom answer 1")).to_have_value("Custom blue")
                page.get_by_role("button", name="Submit answers", exact=True).click()
                expect(page.get_by_role("form", name="Pending questions")).not_to_be_visible()
                page.get_by_role("button", name="Allow once", exact=True).click()
                expect(page.get_by_role("region", name="Pending permission")).not_to_be_visible()
                page.get_by_text("Tool · bash · completed", exact=True).click()
                expect(page.get_by_text("Safe tool output", exact=True)).to_be_visible()
                page.get_by_text("Reasoning", exact=True).click()
                expect(page.get_by_text("Reasoning fixture", exact=True)).to_be_visible()
                assert not page.evaluate("window.workerInjected === true")
                text = "Changed by connected worker"
                expect(
                    page.locator(".oc-message").get_by_text(
                        "Changed by connected worker", exact=True
                    )
                ).to_be_visible()
                assert any(frame.get("type") == "changed" for frame in frames)
                denied = page.evaluate(
                    """async () => (await fetch('/api/commands', {
                      method: 'POST',
                      headers: {'Content-Type':'application/json','X-CSRFToken':'invalid'},
                      body: JSON.stringify({worker:'real-opencode',action:'create',payload:{}})
                    })).status"""
                )
                assert denied == 403
                if screenshots:
                    screenshots.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshots / "opencodes-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                if screenshots:
                    page.screenshot(path=str(screenshots / "opencodes-mobile.png"), full_page=True)
                page.get_by_role("button", name="All tasks", exact=False).click()
                expect(page.get_by_role("button", name="Kanban", exact=True)).to_be_visible()
                page.get_by_role("button", name="List", exact=True).click()
                expect(
                    page.get_by_role(
                        "button", name="Open task: Existing task UI remains available", exact=True
                    )
                ).to_be_visible()
                page.get_by_role(
                    "button", name="Open task: Existing task UI remains available", exact=True
                ).click()
                expect(page.get_by_role("dialog")).to_be_visible()
                page.keyboard.press("Escape")
                page.get_by_role("button", name="OpenCodes", exact=True).click()
                page.reload()
                expect(page.get_by_role("heading", name="Connected OpenCodes.")).to_be_visible()
                check_module_graph(scripts, errors)
                context.close()
                browser.close()
            question = next(
                command for command in interaction_results if command["action"] == "question.reply"
            )
            assert question["payload"]["answers"] == [["Custom blue"], ["Logs", "Metrics"]]
            assert any(
                command["action"] == "permission.reply" and command["payload"]["reply"] == "once"
                for command in interaction_results
            )
            print(
                "PASS real OpenCode create/prompt/history/interrupt, worker health/isolation, "
                "controlled questions/approval, draft preservation, tool/reasoning, "
                "live updates, CSRF/XSS, mobile, task views, production static graph/deep reload; "
                "one entry URL and zero browser errors"
            )
        except Exception:
            if browser_errors:
                print("Browser errors: " + json.dumps(browser_errors), file=sys.stderr)
            log.flush()
            log.seek(0)
            print(log.read()[-6000:], file=sys.stderr)
            raise
        finally:
            stop.set()
            if publisher:
                publisher.join(timeout=20)
            for child in reversed(processes):
                child.terminate()
            for child in reversed(processes):
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument(
        "--assets-only",
        action="store_true",
        help="Production static/lazy-load check without installed OpenCode (CI)",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="atm-opencode-ui-") as temporary:
        run(Path(temporary).resolve(), args.screenshots, args.assets_only)
