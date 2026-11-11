"""Pending approvals inbox across workers for mobile and web (#55)."""

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import Role
from accounts.services import set_role
from coordinator import services
from coordinator.models import Agent

from .test_worker_pending import PERMISSION, QUESTION, post_sync

pytestmark = pytest.mark.django_db


@pytest.fixture
def worker(agent_clients):
    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    services.store_sessions(worker, [{"id": "ses_1", "directory": "/repo", "title": "Fix login"}])
    return worker


def human(django_user_model, role):
    user = django_user_model.objects.create_user(f"{role}-user")
    set_role(user, role, actor="test")
    client = Client()
    client.force_login(user)
    return client


def test_inbox_lists_pending_requests_with_worker_and_session(
    api_client, worker, django_user_model
):
    post_sync(
        api_client,
        {"pending": {"complete": True, "permissions": [PERMISSION], "questions": [QUESTION]}},
    )
    viewer = human(django_user_model, Role.VIEWER)
    page = viewer.get("/api/v1/interactions").json()
    assert page["count"] == 2
    by_kind = {item["kind"]: item for item in page["items"]}
    assert by_kind["permission"]["id"] == "per_a"
    assert by_kind["permission"]["worker"] == "worker-a"
    assert by_kind["permission"]["sessionTitle"] == "Fix login"
    assert by_kind["question"]["questions"][0]["header"] == "Color"


def test_viewer_cannot_answer_but_member_can(api_client, worker, django_user_model):
    post_sync(api_client, {"pending": {"complete": True, "permissions": [PERMISSION]}})
    answer = {
        "worker": "worker-a",
        "action": "permission.reply",
        "sessionID": "ses_1",
        "payload": {"requestID": "per_a", "reply": "once"},
    }
    viewer = human(django_user_model, Role.VIEWER)
    assert (
        viewer.post("/api/v1/commands", answer, content_type="application/json").status_code == 403
    )
    member = human(django_user_model, Role.MEMBER)
    response = member.post("/api/v1/commands", answer, content_type="application/json")
    assert response.status_code == 202, response.content
    item = member.get("/api/v1/interactions").json()["items"][0]
    assert item["replyDispatched"] is True


def test_resolved_requests_leave_the_inbox(api_client, worker, django_user_model):
    post_sync(api_client, {"pending": {"complete": True, "permissions": [PERMISSION]}})
    post_sync(api_client, {"pending": {"complete": True, "permissions": [], "questions": []}})
    viewer = human(django_user_model, Role.VIEWER)
    assert viewer.get("/api/v1/interactions").json() == {"count": 0, "items": []}


def test_agents_see_only_their_worker(agent_clients, worker):
    post_sync(agent_clients[0], {"pending": {"complete": True, "permissions": [PERMISSION]}})
    assert agent_clients[0].get("/api/v1/interactions").json()["count"] == 1
    assert agent_clients[1].get("/api/v1/interactions").json()["count"] == 0
    assert Client().get("/api/v1/interactions").status_code in {401, 403}
