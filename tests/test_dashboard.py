from datetime import timedelta

import anyio
import pytest
from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.db import transaction
from django.test import Client
from django.utils import timezone

from config.asgi import application
from coordinator import services
from coordinator.models import Project, Task

from .test_api import create, post


def connection(client=None, origin="http://localhost"):
    headers = [(b"host", b"localhost"), (b"origin", origin.encode())]
    if client:
        headers.append((b"cookie", client.cookies.output(header="", sep=";").strip().encode()))
    return ApplicationCommunicator(
        application,
        {
            "type": "websocket",
            "path": "/ws/dashboard/",
            "raw_path": b"/ws/dashboard/",
            "headers": headers,
            "query_string": b"",
            "scheme": "ws",
            "subprotocols": [],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
        },
    )


@pytest.fixture
def staff_client(django_user_model):
    user = django_user_model.objects.create_user("dashboard-human", is_staff=True)
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_dashboard_and_snapshot_require_staff_and_do_not_expose_keys(
    client,
    staff_client,
    api_client,
    project,
    django_user_model,
):
    assert client.get("/dashboard/").status_code == 302
    assert api_client.get("/dashboard/state/").status_code == 302
    user = django_user_model.objects.create_user("nonstaff")
    client.force_login(user)
    assert client.get("/dashboard/").status_code == 302
    task = create(api_client, title="<script>untrusted task title</script>")
    post(api_client, f"tasks/{task['id']}/claim")
    assert staff_client.get("/dashboard/").status_code == 200
    response = staff_client.get("/dashboard/state/")
    assert response.status_code == 200
    data = response.json()
    assert data["counts"]["in_progress"] == 1
    assert data["tasks"][0]["claimed_by"] == "alpha"
    assert data["events"][0]["event_type"] == "claimed"
    assert "api_key_hash" not in response.content.decode()
    assert "key_prefix" not in response.content.decode()
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.django_db(transaction=True)
def test_socket_rejects_anonymous_nonstaff_and_untrusted_origin(staff_client, django_user_model):
    ordinary = Client()
    ordinary.force_login(django_user_model.objects.create_user("ordinary-socket"))

    async def verify():
        for client, origin in [
            (None, "http://localhost"),
            (ordinary, "http://localhost"),
            (staff_client, "https://attacker.example"),
        ]:
            socket = connection(client, origin)
            await socket.send_input({"type": "websocket.connect"})
            reply = await socket.receive_output(timeout=5)
            assert reply["type"] == "websocket.close"
            await socket.send_input({"type": "websocket.disconnect", "code": 1000})
            await socket.wait(timeout=5)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
def test_socket_delivers_committed_changes_and_remains_silent_on_rollback(
    staff_client,
    api_client,
    project,
):
    async def verify():
        import json

        socket = connection(staff_client)
        try:
            await socket.send_input({"type": "websocket.connect"})
            assert (await socket.receive_output(timeout=5))["type"] == "websocket.accept"
            assert json.loads((await socket.receive_output())["text"])["type"] == "connected"
            await socket.send_input({"type": "websocket.receive", "text": '{"type":"ping"}'})
            assert json.loads((await socket.receive_output())["text"])["type"] == "pong"
            task = await sync_to_async(create)(api_client, title="Committed dashboard task")
            message = json.loads((await socket.receive_output(timeout=5))["text"])
            assert message["type"] == "changed" and message["event_id"] is not None
            state = await sync_to_async(lambda: staff_client.get("/dashboard/state/").json())()
            assert state["tasks"][0]["id"] == task["id"]

            def rollback():
                with transaction.atomic():
                    services.create_task(None, project, "Must not reach the dashboard")
                    transaction.set_rollback(True)

            await sync_to_async(rollback)()
            assert await socket.receive_nothing(timeout=0.2)
            state = await sync_to_async(lambda: staff_client.get("/dashboard/state/").json())()
            assert state["total_tasks"] == 1
            await sync_to_async(post)(api_client, f"tasks/{task['id']}/claim")
            assert json.loads((await socket.receive_output(timeout=5))["text"])["type"] == "changed"
        finally:
            await socket.send_input({"type": "websocket.disconnect", "code": 1000})
            await socket.wait(timeout=5)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("revocation", ["logout", "expire", "password", "demote"])
def test_socket_closes_when_staff_session_is_invalidated(
    staff_client, django_user_model, revocation
):
    def invalidate():
        if revocation == "logout":
            staff_client.logout()
        elif revocation == "expire":
            from django.contrib.sessions.models import Session

            Session.objects.filter(session_key=staff_client.session.session_key).update(
                expire_date=timezone.now() - timedelta(seconds=1),
            )
        else:
            user = django_user_model.objects.get(username="dashboard-human")
            if revocation == "password":
                user.set_password("changed-test-password")
            else:
                user.is_staff = False
            user.save()

    async def verify():
        socket = connection(staff_client)
        await socket.send_input({"type": "websocket.connect"})
        assert (await socket.receive_output(timeout=5))["type"] == "websocket.accept"
        await socket.receive_output()
        await sync_to_async(invalidate)()
        await socket.send_input({"type": "websocket.receive", "text": '{"type":"ping"}'})
        response = await socket.receive_output(timeout=5)
        assert response == {"type": "websocket.close", "code": 4403}
        await socket.send_input({"type": "websocket.disconnect", "code": 1000})
        await socket.wait(timeout=5)

    anyio.run(verify)


@pytest.mark.django_db(transaction=True)
def test_notification_broker_failure_does_not_fail_a_committed_request(
    api_client, project, monkeypatch
):
    def unavailable(event_id=None, task_id=None, event_type=None):
        raise ConnectionError("Test broker unavailable")

    monkeypatch.setattr("coordinator.signals.publish_change", unavailable)
    task = create(api_client, "Committed despite broker outage")
    detail = api_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["title"] == "Committed despite broker outage"
    assert detail["recent_events"][0]["event_type"] == "created"


@pytest.mark.django_db
def test_project_filter_is_applied_before_dashboard_limits(staff_client, api_client, project):
    Task.objects.bulk_create(
        [
            Task(project=project, title=f"Earlier project task {index}", priority=1)
            for index in range(205)
        ]
    )
    Project.objects.create(slug="later-project", name="Later project")
    task = create(api_client, "Visible in its project", project="later-project")
    state = staff_client.get("/dashboard/state/", {"project": "later-project"}).json()
    assert [item["id"] for item in state["tasks"]] == [task["id"]]
    assert state["scope_total_tasks"] == 1
    assert state["events"][0]["task_id"] == task["id"]
    assert state["projects"][0]["counts"]["pending"] == 205
    assert staff_client.get("/dashboard/state/", {"project": "unknown"}).status_code == 404
