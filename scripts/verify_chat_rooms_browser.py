"""Built dashboard: multi-AI chat rooms, round-robin advance and roles (GH #149).

Run with `uv run --with playwright python scripts/verify_chat_rooms_browser.py`
after `npm run build --prefix frontend`. Uses isolated same-origin fixtures, so
no provider is ever called and nothing is written to a real database.
"""

import functools
import http.server
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT = dict(slug="alpha", name="Alpha", kind="x", active_count=0, docs_path="")
MODELS = [
    "accounts/fireworks/models/minimax-m3p1-flash",
    "accounts/fireworks/models/other-model",
]
REPLIES = ["Use Postgres.", "I disagree, start simpler.", "I will write the tests."]


def participant(index, role, can_act=False, muted=False):
    return dict(
        id=index,
        position=index,
        role=role,
        persona=f"You are the {role}.",
        kind="provider",
        model=dict(providerID="fireworks", modelID=MODELS[(index - 1) % len(MODELS)]),
        worker=None,
        agent="",
        canAct=can_act,
        isMuted=muted,
    )


def message(seq, kind, text, role, tools=()):
    return dict(
        id=seq,
        seq=seq,
        turn=1,
        authorKind=kind,
        participantId=None if kind != "participant" else seq - 1,
        role=role,
        text=text,
        toolEvents=[dict(tool=name, is_error=False) for name in tools],
        model=None if kind != "participant" else dict(providerID="fireworks", modelID=MODELS[0]),
        createdAt="2026-11-25T00:00:00Z",
    )


def room_body(state):
    return dict(
        id=11,
        project="alpha",
        name=state["name"],
        topic="Choose a database",
        status=state["status"],
        roundsPerTurn=1,
        createdBy="owner",
        turn=1 if state["messages"] else 0,
        turnBusy=False,
        pendingSpeakers=list(state["pending"]),
        messageCount=len(state["messages"]),
        historyBytes=100,
        createdAt="2026-11-25T00:00:00Z",
        updatedAt="2026-11-25T00:00:00Z",
        participants=list(state["participants"]),
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
        name="Design review",
        status="open",
        participants=[
            participant(1, "Architect", can_act=True),
            participant(2, "Reviewer"),
            participant(3, "Tester"),
        ],
        messages=[],
        pending=[],
    )
    posted, errors = [], []

    def api(route):
        request = route.request
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        body = {}
        if path == "/api/v1/auth/me":
            body = dict(id=1, username="owner", role=state["role"], email="")
        elif path.endswith("/auth/csrf"):
            body = {"csrfToken": "x"}
        elif path == "/api/v1/meta":
            body = dict(statuses=[], priorities=[])
        elif path == "/api/v1/dashboard/workspace":
            body = dict(
                server_time="2026-11-25T00:00:00Z",
                projects=[PROJECT],
                agents=[],
                events=[],
                total=0,
                counts={},
            )
        elif path == "/api/v1/chat/models":
            body = dict(
                models=[dict(providerID="fireworks", modelID=name) for name in MODELS],
                limits=dict(max_participants=6, max_rounds=3),
            )
        elif path == "/api/v1/chat/rooms":
            if request.method == "POST":
                posted.append(("create_room", request.post_data_json))
                state["name"] = request.post_data_json["name"]
            body = dict(count=1, items=[room_body(state)])
        elif path == "/api/v1/chat/rooms/11/messages":
            if request.method == "POST":
                sent = request.post_data_json
                posted.append(("post", sent))
                state["messages"].append(
                    message(len(state["messages"]) + 1, "human", sent["text"], "owner")
                )
                state["pending"] = [1, 2, 3]
                body = dict(message=state["messages"][-1], room=room_body(state))
            else:
                limit = int(query.get("limit", ["20"])[0])
                page = state["messages"][-limit:]
                body = dict(
                    messages=page,
                    cursor=page[-1]["seq"] if page else 0,
                    olderCursor=None,
                    hasMore=False,
                    messageCount=len(state["messages"]),
                )
        elif path == "/api/v1/chat/rooms/11/advance":
            posted.append(("advance", request.post_data_json))
            if state["pending"]:
                speaker = state["pending"].pop(0)
                role = next(item["role"] for item in state["participants"] if item["id"] == speaker)
                tools = ("get_task",) if speaker == 1 else ()
                state["messages"].append(
                    message(
                        len(state["messages"]) + 1,
                        "participant",
                        REPLIES[speaker - 1],
                        role,
                        tools,
                    )
                )
                body = dict(advanced=True, message=state["messages"][-1], room=room_body(state))
            else:
                body = dict(advanced=False, message=None, room=room_body(state))
        elif path == "/api/v1/chat/rooms/11":
            if request.method == "DELETE":
                state["status"] = "closed"
            body = room_body(state)
        elif path.startswith("/api/v1/chat/rooms/11/participants"):
            posted.append((f"{request.method} participants", request.post_data_json))
            body = state["participants"][0]
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

            def rooms_page():
                page.goto(f"{origin}/?section=chatrooms&project=alpha")
                view = page.locator(".cr-workspace")
                expect(view.locator(".cr-room").first).to_be_visible()
                return view

            # 1. The room, its roster and the one-actor badges are readable.
            view = rooms_page()
            view.locator(".cr-room", has_text="Design review").click()
            expect(view.locator("h1")).to_have_text("Design review")
            expect(view).to_contain_text("Choose a database")
            rail = view.locator(".cr-rail")
            expect(rail.locator(".cr-participant")).to_have_count(3)
            expect(rail.locator(".cr-participant").nth(0)).to_contain_text("Architect")
            expect(rail.locator(".cr-participant").nth(0)).to_contain_text("acts")
            expect(rail.locator(".cr-participant").nth(1)).to_contain_text("reads only")
            expect(rail.locator(".cr-participant").nth(2)).to_contain_text("reads only")
            # Exactly one participant may act.
            expect(rail.locator(".cr-badge.cr-acts")).to_have_count(1)

            # 2. Sending drives one advance per speaker, in position order.
            view.locator(".cr-composer textarea").fill("Which database?")
            view.locator(".cr-composer").get_by_role("button", name="Send").click()
            bubbles = view.locator(".cr-bubble")
            expect(bubbles).to_have_count(4)
            expect(bubbles.nth(0)).to_contain_text("Which database?")
            expect(bubbles.nth(1)).to_contain_text("Architect")
            expect(bubbles.nth(1)).to_contain_text("Use Postgres.")
            expect(bubbles.nth(2)).to_contain_text("Reviewer")
            expect(bubbles.nth(2)).to_contain_text("I disagree, start simpler.")
            expect(bubbles.nth(3)).to_contain_text("Tester")
            expect(bubbles.nth(3)).to_contain_text("I will write the tests.")
            advances = [item for item in posted if item[0] == "advance"]
            assert len(advances) >= 3, advances
            assert ("post", {"text": "Which database?"}) in posted, posted

            # 3. The acting participant's tool use is visible on its message.
            expect(bubbles.nth(1).locator(".cr-tool")).to_contain_text("get_task")
            expect(bubbles.nth(2).locator(".cr-tool")).to_have_count(0)

            # 4. The queue drains, so nobody is left waiting to speak.
            expect(view.locator(".cr-turn-state")).to_contain_text("Nobody is queued")

            # 5. Adding a role posts the chosen model and the acts flag.
            rail.get_by_label("Role").fill("Security")
            rail.get_by_label("Character").fill("You look for abuse paths.")
            rail.get_by_label("Model").select_option(MODELS[1])
            rail.get_by_role("button", name="Add participant").click()
            added = [item for item in posted if item[0] == "POST participants"][-1][1]
            assert added["role"] == "Security", added
            assert added["model"] == {"providerID": "fireworks", "modelID": MODELS[1]}, added
            assert added["canAct"] is False, added

            # 6. Creating a room posts the selected project.
            view.locator(".cr-new-room").get_by_label("New room").fill("Launch review")
            view.locator(".cr-new-room").get_by_role("button", name="Create room").click()
            created = [item for item in posted if item[0] == "create_room"][-1][1]
            assert created == {"project": "alpha", "name": "Launch review"}, created

            # 7. Closing the room hides the composer but keeps the transcript.
            # Reload first: creating a room refetches the list, so the previous
            # render's controls are detached.
            view = rooms_page()
            view.locator(".cr-room").first.click()
            expect(view.locator(".cr-bubble").first).to_be_visible()
            view.get_by_role("button", name="Close room").click()
            expect(view.locator(".cr-composer")).to_have_count(0)
            expect(view.locator(".cr-bubble")).to_have_count(4)

            # 8. A viewer reads the transcript but gets no write control at all.
            state["status"] = "open"
            state["role"] = "viewer"
            view = rooms_page()
            view.locator(".cr-room").first.click()
            expect(view.locator(".cr-bubble").first).to_be_visible()
            expect(view.locator(".cr-composer")).to_have_count(0)
            expect(view.locator(".cr-new-room")).to_have_count(0)
            expect(view.locator(".cr-add-participant")).to_have_count(0)
            expect(view.get_by_role("button", name="Close room")).to_have_count(0)
            expect(view.get_by_role("button", name="Let this one act")).to_have_count(0)

            # 9. Mobile keeps the transcript, roster and composer reachable.
            state["role"] = "owner"
            page.set_viewport_size(dict(width=390, height=844))
            view = rooms_page()
            view.locator(".cr-room").first.click()
            expect(view.locator(".cr-bubble").first).to_be_visible()
            expect(view.locator(".cr-rail")).to_be_visible()
            expect(view.locator(".cr-composer textarea")).to_be_visible()

            browser.close()
    finally:
        server.shutdown()
    assert not errors, errors
    print("Chat rooms browser checks passed")


if __name__ == "__main__":
    main()
