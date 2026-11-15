"""Dashboard WebSocket auth for sessions and mobile device tokens (#51)."""

import anyio
import pytest
from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.core.cache import cache
from django.test import Client

from accounts.models import Role
from accounts.services import set_role
from config.asgi import application

PASSWORD = "a-Socket-passphrase-31"


def socket(subprotocols=(), headers=(), cookie_client=None):
    raw_headers = [(b"host", b"localhost"), *headers]
    if cookie_client:
        cookie = cookie_client.cookies.output(header="", sep=";").strip().encode()
        raw_headers.append((b"cookie", cookie))
    return ApplicationCommunicator(
        application,
        {
            "type": "websocket",
            "path": "/ws/dashboard/",
            "raw_path": b"/ws/dashboard/",
            "headers": raw_headers,
            "query_string": b"",
            "scheme": "ws",
            "subprotocols": list(subprotocols),
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
        },
    )


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def member(django_user_model):
    user = django_user_model.objects.create_user("mobile", password=PASSWORD)
    set_role(user, Role.MEMBER, actor="test")
    return user


def device_token(name="phone"):
    response = Client().post(
        "/api/v1/auth/tokens",
        {"username": "mobile", "password": PASSWORD, "name": name},
        content_type="application/json",
    )
    assert response.status_code == 201
    return response.json()["token"]


def ticket_for(raw_token):
    response = Client(HTTP_AUTHORIZATION=f"Token {raw_token}").post("/api/v1/realtime/ticket")
    assert response.status_code == 200, response.content
    return response.json()


async def opens(subprotocols, headers=(), cookie_client=None):
    communicator = socket(subprotocols, headers, cookie_client)
    await communicator.send_input({"type": "websocket.connect"})
    reply = await communicator.receive_output(timeout=5)
    if reply["type"] == "websocket.accept":
        await communicator.receive_output(timeout=5)  # {"type": "connected"}
    return communicator, reply


async def close(communicator):
    await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
    await communicator.wait(timeout=5)


@pytest.mark.django_db(transaction=True)
def test_device_token_ticket_opens_the_socket_without_origin(member):
    issued = ticket_for(device_token())
    assert issued["expires_in"] == 60
    assert issued["subprotocols"][0] == "atm.v1"

    async def verify():
        communicator, reply = await opens(issued["subprotocols"])
        assert (reply["type"], reply.get("subprotocol")) == ("websocket.accept", "atm.v1")
        await communicator.send_input({"type": "websocket.receive", "text": '{"type":"ping"}'})
        assert (await communicator.receive_output(timeout=5))["text"] == '{"type": "pong"}'
        await close(communicator)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
def test_tickets_work_once_and_unknown_tickets_are_refused(member):
    issued = ticket_for(device_token())

    async def verify():
        first, reply = await opens(issued["subprotocols"])
        assert reply["type"] == "websocket.accept"
        await close(first)
        replay, reply = await opens(issued["subprotocols"])
        assert reply == {"type": "websocket.close", "code": 4403}
        await close(replay)
        forged, reply = await opens(["atm.v1", "atm.ticket.not-a-real-ticket"])
        assert reply == {"type": "websocket.close", "code": 4403}
        await close(forged)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
def test_revoking_the_token_closes_an_open_socket(member):
    raw = device_token()
    issued = ticket_for(raw)

    async def verify():
        communicator, reply = await opens(issued["subprotocols"])
        assert reply["type"] == "websocket.accept"
        revoked = await sync_to_async(
            lambda: Client(HTTP_AUTHORIZATION=f"Token {raw}").post(
                "/api/v1/auth/tokens/current/revoke"
            )
        )()
        assert revoked.status_code == 200
        await communicator.send_input({"type": "websocket.receive", "text": '{"type":"ping"}'})
        assert await communicator.receive_output(timeout=5) == {
            "type": "websocket.close",
            "code": 4403,
        }
        await close(communicator)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
def test_deactivation_closes_the_socket_and_blocks_new_tickets(member):
    raw = device_token()
    issued = ticket_for(raw)

    async def verify():
        communicator, reply = await opens(issued["subprotocols"])
        assert reply["type"] == "websocket.accept"

        def deactivate():
            member.is_active = False
            member.save()

        await sync_to_async(deactivate)()
        await communicator.send_input({"type": "websocket.receive", "text": '{"type":"ping"}'})
        assert (await communicator.receive_output(timeout=5))["type"] == "websocket.close"
        await close(communicator)

    anyio.run(verify)
    denied = Client(HTTP_AUTHORIZATION=f"Token {raw}").post("/api/v1/realtime/ticket")
    assert denied.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_browser_sessions_still_need_a_trusted_origin(member):
    browser = Client()
    browser.force_login(member)

    async def verify():
        trusted, reply = await opens([], [(b"origin", b"http://localhost")], browser)
        assert reply["type"] == "websocket.accept" and reply.get("subprotocol") is None
        await close(trusted)
        hostile, reply = await opens([], [(b"origin", b"https://attacker.example")], browser)
        assert reply["type"] == "websocket.close"
        await close(hostile)
        anonymous, reply = await opens([], [(b"origin", b"http://localhost")])
        assert reply["type"] == "websocket.close"
        await close(anonymous)

    anyio.run(verify)


@pytest.mark.django_db
def test_tickets_need_a_signed_in_human(member, api_client):
    assert Client().post("/api/v1/realtime/ticket").status_code in {401, 403}
    assert api_client.post("/api/v1/realtime/ticket").status_code == 403  # agent keys
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(member)
    # Session callers must send CSRF, so a hostile page cannot mint tickets.
    assert browser.post("/api/v1/realtime/ticket").status_code == 403


@pytest.mark.django_db
def test_raw_ticket_is_not_stored(member):
    issued = ticket_for(device_token())
    ticket = issued["ticket"]
    assert cache.get(f"ws-ticket:{ticket}") is None  # stored under its hash only
