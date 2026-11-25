"""Chat room realtime: one post-commit notice per message, broker-safe (GH #149)."""

import json

import anyio
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client

from accounts.models import UserProfile
from coordinator import chat_runtime
from coordinator.models import ChatMessage, ChatParticipant, ChatRoom, Project

from .test_dashboard import connection


def seed_room(name="Live room"):
    """A room with one participant, built without going through the API."""
    project = Project.objects.create(slug=f"live-{name.lower().replace(' ', '-')}", name=name)
    owner = get_user_model().objects.create_user(f"owner-{project.slug}", is_staff=True)
    UserProfile.objects.update_or_create(user=owner, defaults={"role": "owner"})
    room = ChatRoom.objects.create(project=project, name=name, created_by=owner)
    ChatParticipant.objects.create(
        room=room,
        position=1,
        role="Architect",
        model={"providerID": "fireworks", "modelID": "m"},
    )
    return room, owner


@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_user("chat-live-human", is_staff=True)
    UserProfile.objects.update_or_create(user=user, defaults={"role": "owner"})
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db(transaction=True)
def test_a_committed_room_message_pushes_one_notice_carrying_the_room(staff_client):
    room, owner = seed_room()

    async def verify():
        socket = connection(staff_client)
        try:
            await socket.send_input({"type": "websocket.connect"})
            assert (await socket.receive_output(timeout=5))["type"] == "websocket.accept"
            assert json.loads((await socket.receive_output())["text"])["type"] == "connected"

            await sync_to_async(chat_runtime.post_message)(room, owner, "Hello room")
            message = json.loads((await socket.receive_output(timeout=5))["text"])
            assert message["type"] == "changed"
            assert message["event_type"] == "chat.message.created"
            assert message["subject"] == "chatroom"
            assert message["room_id"] == room.pk
            # The notice is an invalidation, never the message text itself.
            assert "Hello room" not in json.dumps(message)

            def rollback():
                with transaction.atomic():
                    fresh = ChatRoom.objects.get(pk=room.pk)
                    chat_runtime.append_message(
                        fresh,
                        author_kind=ChatMessage.Author.SYSTEM,
                        text="Must not be announced",
                    )
                    transaction.set_rollback(True)

            await sync_to_async(rollback)()
            assert await socket.receive_nothing(timeout=0.2)
        finally:
            await socket.send_input({"type": "websocket.disconnect", "code": 1000})
            await socket.wait(timeout=5)

    anyio.run(verify)


@pytest.mark.django_db
def test_broker_failure_never_fails_a_committed_room_message(monkeypatch):
    room, owner = seed_room("Broker room")

    def unavailable(*args, **kwargs):
        raise ConnectionError("Test broker unavailable")

    monkeypatch.setattr("coordinator.signals.dispatch_notification", unavailable)
    _, message = chat_runtime.post_message(room, owner, "Committed despite broker outage")
    assert message.text == "Committed despite broker outage"
    assert ChatMessage.objects.filter(pk=message.pk).exists()
    assert ChatRoom.objects.get(pk=room.pk).message_count == 1


@pytest.mark.django_db
def test_updating_a_message_does_not_re_announce_it(monkeypatch):
    room, owner = seed_room("Quiet room")
    _, message = chat_runtime.post_message(room, owner, "Once")
    notices = []
    monkeypatch.setattr(
        "coordinator.signals.dispatch_notification",
        lambda *args, **kwargs: notices.append(kwargs.get("subject")),
    )
    message.tool_events = [{"tool": "get_task", "is_error": False}]
    message.save(update_fields=["tool_events"])
    assert notices == [], "only created messages announce; updates are not re-pushed"
