"""Role permission matrix (#43): anonymous, agent key, viewer, member, owner.

Viewers read; members create/edit tasks, command workers and plan; owners also
register agents. Agent Bearer keys keep their own per-endpoint rules.
"""

import pytest
from django.test import Client

from accounts.models import Role
from accounts.services import set_role
from coordinator.models import Task

ANON, AGENT, VIEWER, MEMBER, OWNER = "anonymous", "agent", "viewer", "member", "owner"
ACTORS = [ANON, AGENT, VIEWER, MEMBER, OWNER]
DENIED = {401, 403}


@pytest.fixture
def clients(api_client, django_user_model, project):
    owner = django_user_model.objects.create_user("owner-user", is_superuser=True, is_staff=True)
    clients = {ANON: Client(), AGENT: api_client}
    for name, role in [(VIEWER, Role.VIEWER), (MEMBER, Role.MEMBER), (OWNER, Role.OWNER)]:
        user = owner if role == Role.OWNER else django_user_model.objects.create_user(name)
        set_role(user, role, actor="test")
        client = Client()
        client.force_login(user)
        clients[name] = client
    return clients


@pytest.fixture
def task(project):
    return Task.objects.create(project=project, title="Matrix task")


def call(client, method, path, body=None):
    if method == "get":
        return client.get(path)
    return getattr(client, method)(path, body or {}, content_type="application/json")


def allowed(response):
    return response.status_code not in DENIED


# (method, path template, actors allowed through the permission layer)
MATRIX = [
    ("get", "/api/v1/tasks", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/tasks/{task}", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/overview", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/workers", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/sessions", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/commands", {AGENT, VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/dashboard/state", {VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/dashboard/workspace", {VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/projects/coordinator/planning", {VIEWER, MEMBER, OWNER}),
    ("get", "/api/v1/schema", {AGENT, VIEWER, MEMBER, OWNER}),
    ("post", "/api/v1/tasks", {AGENT, MEMBER, OWNER}),
    # Agents edit only tasks they have claimed; this task is unclaimed.
    ("patch", "/api/v1/tasks/{task}", {MEMBER, OWNER}),
    ("post", "/api/v1/tasks/{task}/notes", {AGENT, MEMBER, OWNER}),
    ("post", "/api/v1/commands", {AGENT, MEMBER, OWNER}),
    ("post", "/api/v1/commands/999999/resolve", {MEMBER, OWNER}),
    ("post", "/api/v1/projects", {MEMBER, OWNER}),
    ("post", "/api/v1/projects/coordinator/planning", {MEMBER, OWNER}),
    ("get", "/api/v1/agents", {OWNER}),
    ("post", "/api/v1/agents", {OWNER}),
]

BODIES = {
    ("post", "/api/v1/tasks"): {"title": "From matrix", "project": "coordinator"},
    ("patch", "/api/v1/tasks/{task}"): {"priority": 7},
    ("post", "/api/v1/tasks/{task}/notes"): {"message": "matrix note"},
    ("post", "/api/v1/commands"): {"worker": "missing", "action": "abort"},
    ("post", "/api/v1/commands/999999/resolve"): {"resolution": "error"},
    ("post", "/api/v1/projects"): {"slug": "matrix-project", "name": "Matrix"},
    ("post", "/api/v1/projects/coordinator/planning"): {"revision": 0, "operation": "settings"},
    ("post", "/api/v1/agents"): {"name": "matrix-agent"},
}


@pytest.mark.django_db
@pytest.mark.parametrize(("method", "template", "permitted"), MATRIX)
def test_permission_matrix(clients, task, method, template, permitted):
    path = template.format(task=task.pk)
    for actor in ACTORS:
        response = call(clients[actor], method, path, BODIES.get((method, template)))
        assert allowed(response) == (actor in permitted), (
            f"{actor} {method.upper()} {path} -> {response.status_code}"
        )


@pytest.mark.django_db
def test_human_actor_label_is_still_recorded(clients, task):
    response = call(clients[MEMBER], "post", f"/api/v1/tasks/{task.pk}/notes", {"message": "hi"})
    assert response.status_code == 201
    event = task.events.order_by("-pk").first()
    assert event.agent_id is None
    assert "member" in event.message and "hi" in event.message


@pytest.mark.django_db
def test_viewer_cannot_mutate_even_through_legacy_mount(clients, task):
    response = call(clients[VIEWER], "post", f"/api/tasks/{task.pk}/notes", {"message": "x"})
    assert response.status_code == 403
    assert not task.events.filter(message__contains="x").exists()
