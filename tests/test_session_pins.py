"""Task #412: workspace-wide pins persisted independently of client storage."""

import json
from datetime import timedelta

import anyio
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from accounts.models import Role, UserProfile
from coordinator import services
from coordinator.models import Agent, WorkerSession

from .test_dashboard import connection

pytestmark = pytest.mark.django_db


class StubLayer:
    def __init__(self):
        self.messages = []

    async def group_send(self, group, message):
        self.messages.append((group, message))


@pytest.fixture
def layer(monkeypatch):
    stub = StubLayer()
    monkeypatch.setattr("coordinator.realtime.get_channel_layer", lambda: stub)
    return stub


def human(username, role):
    user = get_user_model().objects.create_user(username, password="test-password")
    UserProfile.objects.update_or_create(user=user, defaults={"role": role})
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def sessions():
    agent = Agent(name="pins-agent")
    key = agent.set_new_api_key()
    agent.save()
    worker = services.provision_worker("pins-worker", agent)
    other = Agent(name="other-agent")
    other.set_new_api_key()
    other.save()
    second = services.provision_worker("other-worker", other)
    return (
        WorkerSession.objects.create(worker=worker, remote_id="ses_one", title="One"),
        WorkerSession.objects.create(worker=worker, remote_id="ses_two", title="Two"),
        WorkerSession.objects.create(worker=second, remote_id="ses_one", title="Other"),
        key,
    )


def pin(client, worker, session, pinned=True):
    return client.post(
        "/api/v1/session-pins",
        {"worker": worker, "sessionID": session, "pinned": pinned},
        content_type="application/json",
    )


def test_member_pins_are_workspace_wide_and_ordered(sessions):
    member = human("pin-member", Role.MEMBER)
    assert pin(member, "pins-worker", "ses_one").status_code == 200
    assert pin(member, "pins-worker", "ses_two").status_code == 200
    assert pin(member, "other-worker", "ses_one").status_code == 200

    # Another account and a fresh client see the same workspace pins.
    listed = human("pin-colleague", Role.VIEWER)
    rows = listed.get("/api/v1/session-pins").json()["items"]
    assert [(row["worker"], row["sessionID"]) for row in rows] == [
        ("other-worker", "ses_one"),
        ("pins-worker", "ses_two"),
        ("pins-worker", "ses_one"),
    ]
    assert rows[0]["title"] == "Other"

    # Re-pinning is a set operation: no duplicate rows and no reordering.
    assert pin(member, "other-worker", "ses_one").status_code == 200
    again = listed.get("/api/v1/session-pins").json()["items"]
    assert [(row["worker"], row["sessionID"]) for row in again] == [
        ("other-worker", "ses_one"),
        ("pins-worker", "ses_two"),
        ("pins-worker", "ses_one"),
    ]
    assert WorkerSession.objects.filter(pinned_at__isnull=False).count() == 3

    assert pin(member, "pins-worker", "ses_two", False).status_code == 200
    remaining = listed.get("/api/v1/session-pins").json()["items"]
    assert ("pins-worker", "ses_two") not in [
        (row["worker"], row["sessionID"]) for row in remaining
    ]
    # Unpin is idempotent.
    assert pin(member, "pins-worker", "ses_two", False).status_code == 200


def test_pins_survive_restart_by_living_in_the_database(sessions):
    one, _, _, _ = sessions
    member = human("pin-member", Role.MEMBER)
    assert pin(member, "pins-worker", "ses_one").status_code == 200
    # A fresh read cycle re-reads the persisted row rather than client state.
    one.refresh_from_db()
    assert one.pinned_at is not None
    assert timezone.now() - one.pinned_at < timedelta(minutes=1)
    assert pin(member, "pins-worker", "ses_one", False).status_code == 200
    one.refresh_from_db()
    assert one.pinned_at is None


def test_viewer_reads_but_cannot_pin_and_anonymous_is_denied(sessions):
    viewer = human("pin-viewer", Role.VIEWER)
    assert viewer.get("/api/v1/session-pins").status_code == 200
    assert pin(viewer, "pins-worker", "ses_one").status_code == 403
    assert Client().get("/api/v1/session-pins").status_code in (401, 403)
    assert pin(Client(), "pins-worker", "ses_one").status_code in (401, 403)


def test_agent_keys_are_refused(sessions):
    *_, key = sessions
    agent = Client(HTTP_AUTHORIZATION=f"Bearer {key}")
    assert agent.get("/api/v1/session-pins").status_code == 403
    assert pin(agent, "pins-worker", "ses_one").status_code == 403


def test_unknown_sessions_and_invalid_bodies_are_rejected(sessions):
    member = human("pin-member", Role.MEMBER)
    assert pin(member, "pins-worker", "missing").status_code == 404
    assert pin(member, "missing-worker", "ses_one").status_code == 404
    assert (
        member.post(
            "/api/v1/session-pins", {"worker": "pins-worker"}, content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        member.post(
            "/api/v1/session-pins",
            {"worker": "pins-worker", "sessionID": "ses_one", "pinned": "maybe"},
            content_type="application/json",
        ).status_code
        == 400
    )


def test_csrf_is_required_for_browser_writes(sessions):
    human("pin-member", Role.MEMBER)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(get_user_model().objects.get(username="pin-member"))
    assert pin(strict, "pins-worker", "ses_one").status_code == 403


def test_changes_notify_other_clients_after_commit(
    sessions, layer, django_capture_on_commit_callbacks
):
    member = human("pin-member", Role.MEMBER)
    with django_capture_on_commit_callbacks(execute=True):
        assert pin(member, "pins-worker", "ses_one").status_code == 200
    assert any(
        message.get("event_type") == "session.pins.changed"
        and message.get("subject") == "session_pins"
        for _, message in layer.messages
    )
    layer.messages.clear()
    # No change, no notification.
    with django_capture_on_commit_callbacks(execute=True):
        assert pin(member, "pins-worker", "ses_one").status_code == 200
    assert layer.messages == []


def test_observations_never_clear_a_pin(sessions):
    one, _, _, _ = sessions
    member = human("pin-member", Role.MEMBER)
    assert pin(member, "pins-worker", "ses_one").status_code == 200
    one.refresh_from_db()
    pinned_at = one.pinned_at
    # A worker sync snapshot for the same session arrives afterwards.
    services.upsert_session(
        one.worker,
        {
            "sessionID": "ses_one",
            "title": "Observed title",
            "status": "busy",
            "time": {"created": 1761048000000, "updated": 1761049000000},
        },
    )
    one.refresh_from_db()
    assert one.pinned_at == pinned_at
    assert one.title == "Observed title"


def test_retention_pruning_keeps_pinned_sessions(sessions, settings):
    one, two, _, _ = sessions
    settings.WORKER_SESSION_KEEP = 1
    member = human("pin-member", Role.MEMBER)
    assert pin(member, "pins-worker", "ses_one").status_code == 200
    WorkerSession.objects.create(worker=two.worker, remote_id="ses_three", title="Three")
    services.apply_observation_retention()
    assert WorkerSession.objects.filter(pk=one.pk).exists()
    assert not WorkerSession.objects.filter(pk=two.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_other_clients_receive_pin_changes_over_the_dashboard_socket(sessions):
    """Real ASGI transport: a pin write reaches every connected client."""

    async def verify():
        member = await sync_to_async(human)("pin-socket-member", Role.MEMBER)
        viewer = await sync_to_async(human)("pin-socket-viewer", Role.VIEWER)
        sockets = [connection(member), connection(viewer)]
        try:
            for socket in sockets:
                await socket.send_input({"type": "websocket.connect"})
                assert (await socket.receive_output(timeout=5))["type"] == "websocket.accept"
                assert json.loads((await socket.receive_output())["text"])["type"] == "connected"
            assert (await sync_to_async(pin)(member, "pins-worker", "ses_one")).status_code == 200
            for socket in sockets:
                notice = json.loads((await socket.receive_output(timeout=5))["text"])
                assert notice["type"] == "changed"
                assert notice["event_type"] == "session.pins.changed"
                assert notice["subject"] == "session_pins"
            listed = await sync_to_async(lambda: viewer.get("/api/v1/session-pins").json())()
            assert [(row["worker"], row["sessionID"]) for row in listed["items"]] == [
                ("pins-worker", "ses_one")
            ]
        finally:
            for socket in sockets:
                await socket.send_input({"type": "websocket.disconnect", "code": 1000})
                await socket.wait(timeout=5)

    anyio.run(verify)
