"""Every task, however created, appears on its project's planning board."""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from coordinator import services
from coordinator.models import Agent, PlanningCard, Project, ProjectPlan, Task


def create(client, title, **kwargs):
    response = client.post(
        "/api/tasks",
        {"title": title, "project": "coordinator", **kwargs},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    return response.json()["id"]


def post(client, path, data=None):
    response = client.post(f"/api/{path}", data or {}, content_type="application/json")
    assert response.status_code == 200, response.content


def board(slug="coordinator"):
    staff = Client()
    staff.force_login(User.objects.get_or_create(username="viewer", is_staff=True)[0])
    response = staff.get(f"/api/projects/{slug}/planning")
    assert response.status_code == 200, response.content
    return {c["task"]: c for c in response.json()["cards"]}


@pytest.mark.django_db
def test_agent_created_task_appears_on_board_and_follows_status(api_client, project):
    first = create(api_client, "First", description="Details")
    waiting = create(api_client, "Waiting", depends_on=[first])
    cards = board()
    assert cards[first]["column"] == "Ready"
    assert cards[first]["title"] == "First"
    assert cards[first]["description"] == "Details"
    assert cards[first]["task_status"] == "pending"
    assert cards[waiting]["column"] == "Backlog"

    post(api_client, f"tasks/{first}/claim")
    assert board()[first]["column"] == "In progress"
    post(api_client, f"tasks/{first}/release", {"reason": "handoff"})
    assert board()[first]["column"] == "Ready"
    post(api_client, f"tasks/{first}/claim")
    post(api_client, f"tasks/{first}/status", {"status": "done", "result_summary": "Shipped"})
    cards = board()
    assert cards[first]["column"] == "Completed"
    assert cards[first]["task_status"] == "done"
    assert PlanningCard.objects.filter(task_id=first).count() == 1


@pytest.mark.django_db
def test_custom_columns_keep_operator_placement(project):
    agent = Agent.objects.create(name="solo")
    ProjectPlan.objects.create(project=project, columns=["Todo", "Doing"])
    task = services.create_task(agent, project, "Custom board")
    assert board()[task.pk]["column"] == "Todo"
    PlanningCard.objects.filter(task=task).update(column="Doing")
    services.claim_task(agent, task.pk)
    assert board()[task.pk]["column"] == "Doing"


@pytest.mark.django_db
def test_admin_project_move_moves_card(project):
    other = Project.objects.create(slug="other", name="Other")
    task = services.create_task(Agent.objects.create(name="solo"), project, "Move me")
    task.project = other
    services.admin_save_task(task, ["project"], [], "root")
    assert board("other")[task.pk]["column"] == "Ready"
    assert task.pk not in board()


@pytest.mark.django_db(transaction=True)
def test_backfill_creates_cards_for_existing_tasks():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("coordinator", "0017_voice_conversations")]
    try:
        executor.migrate(previous)
        apps = executor.loader.project_state(previous).apps
        OldProject = apps.get_model("coordinator", "Project")
        OldTask = apps.get_model("coordinator", "Task")
        Plan = apps.get_model("coordinator", "ProjectPlan")
        Card = apps.get_model("coordinator", "PlanningCard")
        project = OldProject.objects.create(slug="legacy", name="Legacy")
        done = OldTask.objects.create(project=project, title="Old done", status="done")
        pending = OldTask.objects.create(project=project, title="Old pending")
        linked = OldTask.objects.create(project=project, title="Queued", status="review")
        plan = Plan.objects.create(project=project)
        Card.objects.create(plan=plan, title="Queued", task=linked, column="Ideas")
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        columns = dict(
            PlanningCard.objects.filter(plan__project__slug="legacy").values_list(
                "task_id", "column"
            )
        )
        assert columns == {done.pk: "Completed", pending.pk: "Ready", linked.pk: "Ideas"}
    finally:
        MigrationExecutor(connection).migrate(latest)
        PlanningCard.objects.all().delete()
        ProjectPlan.objects.all().delete()
        Task.objects.all().delete()
        Project.objects.all().delete()
