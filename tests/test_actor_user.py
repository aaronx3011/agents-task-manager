"""Human actors are recorded as user FKs next to their text labels (#44)."""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from coordinator import services
from coordinator.models import (
    Agent,
    PlanningCard,
    PlanningJob,
    ProjectPlan,
    Task,
    TaskEvent,
    WorkerCommand,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def human(django_user_model):
    user = django_user_model.objects.create_user("operator", is_staff=True)  # member
    client = Client()
    client.force_login(user)
    return user, client


@pytest.fixture
def worker(agent_clients):
    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.default_model = {"providerID": "test", "modelID": "model"}
    worker.model_catalog = [worker.default_model]
    worker.save()
    return worker


def post(client, path, body=None, method="post"):
    return getattr(client, method)(f"/api/v1/{path}", body or {}, content_type="application/json")


def latest(task_id, event_type):
    return TaskEvent.objects.filter(task_id=task_id, event_type=event_type).latest("pk")


def test_every_human_task_mutation_records_the_user(human, project, agent_clients):
    user, client = human
    created = post(client, "tasks", {"title": "Human task", "project": "coordinator"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert latest(task_id, "created").actor_user == user

    assert post(client, f"tasks/{task_id}", {"priority": 3}, method="patch").status_code == 200
    assert post(client, f"tasks/{task_id}/notes", {"message": "note"}).status_code == 201
    notes = TaskEvent.objects.filter(task_id=task_id, event_type="note")
    assert {event.actor_user for event in notes} == {user}

    post(agent_clients[0], f"tasks/{task_id}/claim")
    assert latest(task_id, "claimed").actor_user is None  # agent action
    assert post(client, f"tasks/{task_id}/heartbeat").status_code == 200
    assert latest(task_id, "heartbeat").actor_user == user
    assert post(client, f"tasks/{task_id}/release", {"reason": "reassign"}).status_code == 200
    assert latest(task_id, "released").actor_user == user
    status = post(client, f"tasks/{task_id}/status", {"status": "cancelled", "reason": "dup"})
    assert status.status_code == 200
    changed = latest(task_id, "status_changed")
    assert changed.actor_user == user
    assert changed.message.startswith("Human staff operator: ")  # label kept for history


def test_agent_mutations_leave_the_user_empty(api_client, project):
    task_id = post(api_client, "tasks", {"title": "Agent task", "project": "coordinator"}).json()[
        "id"
    ]
    post(api_client, f"tasks/{task_id}/notes", {"message": "agent note"})
    assert not TaskEvent.objects.filter(task_id=task_id, actor_user__isnull=False).exists()


def test_admin_task_edits_record_the_user(project):
    root = User.objects.create_superuser("root", password="pw")
    client = Client()
    client.force_login(root)
    task = Task.objects.create(project=project, title="Admin edit")
    response = client.post(
        f"/admin/coordinator/task/{task.pk}/change/",
        {
            "title": "Admin edited",
            "description": "",
            "project": project.pk,
            "status": "pending",
            "priority": 5,
            "blocked_reason": "",
            "result_summary": "",
            "events-TOTAL_FORMS": "0",
            "events-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:500]
    event = TaskEvent.objects.filter(task=task).latest("pk")
    assert event.actor_user == root and event.message.startswith("Human admin root:")


def test_human_commands_and_launches_record_the_user(human, worker, project, agent_clients):
    user, client = human
    created = post(client, "commands", {"worker": "worker-a", "action": "create", "payload": {}})
    assert created.status_code == 202, created.content
    assert WorkerCommand.objects.get(pk=created.json()["id"]).actor_user == user

    task = services.create_task(Agent.objects.get(name="alpha"), project, "Launch me")
    services.claim_task(Agent.objects.get(name="alpha"), task.pk)
    launched = post(client, f"tasks/{task.pk}/launch", {"worker": "worker-a"})
    assert launched.status_code == 201, launched.content
    command = WorkerCommand.objects.get(pk=launched.json()["createCommand"]["id"])
    assert command.actor_user == user and command.created_by.name == "alpha"
    assert latest(task.pk, "execution.launched").actor_user == user

    agent_command = post(
        agent_clients[0], "commands", {"worker": "worker-a", "action": "create", "payload": {}}
    )
    assert WorkerCommand.objects.get(pk=agent_command.json()["id"]).actor_user is None


def test_planning_mutations_record_the_user(human, worker, project):
    user, client = human
    plan = ProjectPlan.objects.create(project=project, default_worker=worker, directory="/w")
    linked = Task.objects.create(project=project, title="Linked")
    card = PlanningCard.objects.create(plan=plan, title="Card", task=linked)

    def mutate(operation, **values):
        plan.refresh_from_db()
        return post(
            client,
            f"projects/{project.slug}/planning",
            {"revision": plan.revision, "operation": operation, **values},
        )

    assert (
        mutate(
            "card",
            card={
                "id": card.pk,
                "title": "Card v2",
                "description": "",
                "column": card.column,
                "labels": [],
                "checklist": [],
                "instructions": "",
            },
        ).status_code
        == 200
    )
    assert latest(linked.pk, "note").actor_user == user
    assert mutate("enqueue", card_id=card.pk).status_code == 200
    assert PlanningJob.objects.get(card=card).actor_user == user


def test_notifications_still_follow_commit(
    human, project, monkeypatch, django_capture_on_commit_callbacks
):
    _, client = human
    sent = []
    monkeypatch.setattr(
        "coordinator.signals.publish_change", lambda *args, **kwargs: sent.append("changed")
    )
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        post(client, "tasks", {"title": "Notify", "project": "coordinator"})
        assert sent == []
    for callback in callbacks:
        callback()
    assert sent


@pytest.mark.django_db(transaction=True)
def test_backfill_links_labels_that_match_users():
    executor = MigrationExecutor(connection)
    executor.migrate([("coordinator", "0011_actor_user")])
    apps = executor.loader.project_state([("coordinator", "0011_actor_user")]).apps
    HUser = apps.get_model("auth", "User")
    HProject = apps.get_model("coordinator", "Project")
    HTask = apps.get_model("coordinator", "Task")
    HEvent = apps.get_model("coordinator", "TaskEvent")
    ana = HUser.objects.create(username="ana", password="!")
    task = HTask.objects.create(project=HProject.objects.create(slug="p", name="P"), title="t")
    staff_event = HEvent.objects.create(task=task, event_type="note", message="Human staff ana: hi")
    admin_event = HEvent.objects.create(
        task=task, event_type="note", message="Human admin ana: updated title"
    )
    ghost = HEvent.objects.create(task=task, event_type="note", message="Human staff gone: hi")
    plain = HEvent.objects.create(task=task, event_type="note", message="Agent note")

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([("coordinator", "0012_backfill_actor_user")])

    linked = dict(TaskEvent.objects.values_list("pk", "actor_user_id"))
    assert linked[staff_event.pk] == ana.pk and linked[admin_event.pk] == ana.pk
    assert linked[ghost.pk] is None and linked[plain.pk] is None
