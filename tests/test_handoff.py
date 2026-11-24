"""Semi-automatic hand-off mode (#431)."""

import pytest
from django.test import override_settings
from django.utils import timezone

from coordinator import handoff, services
from coordinator.models import (
    Agent,
    Project,
    Task,
    TaskEvent,
    TaskExecutionAttempt,
    WorkerCommand,
    WorkerEvent,
)

from .test_worker_pending import PERMISSION, QUESTION, post_sync

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(agent_clients):
    agent = Agent.objects.get(name="alpha")
    project = Project.objects.create(slug="coordinator", name="Coordinator")
    task = services.create_task(agent=agent, project=project, title="T")
    worker = services.provision_worker("worker-a", agent)
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    services.store_sessions(worker, [{"id": "ses_1", "directory": "/repo", "title": "T1"}])
    TaskExecutionAttempt.objects.create(
        task=task,
        owner=agent,
        worker=worker,
        remote_session_id="ses_1",
        outcome=TaskExecutionAttempt.Outcome.ACCEPTED,
    )
    return task, worker


def enable(task):
    Task.objects.filter(pk=task.pk).update(handoff_mode=Task.HandoffMode.SEMI_AUTO)


def replies(worker):
    return WorkerCommand.objects.filter(worker=worker, action="permission.reply")


def perm(**extra):
    return {**PERMISSION, **extra}


def ingest(worker, permissions=(), questions=()):
    services.ingest_pending_interactions(
        worker, {"complete": True, "permissions": list(permissions), "questions": list(questions)}
    )


def test_flag_defaults_off_and_persists(setup, api_client):
    task, _ = setup
    assert Task.objects.get(pk=task.pk).handoff_mode == "off"
    created = api_client.post(
        "/api/v1/tasks",
        {"title": "x", "project": "coordinator", "handoff_mode": "semi_auto"},
        content_type="application/json",
    )
    assert created.status_code == 201
    assert created.json()["handoff_mode"] == "semi_auto"
    assert Task.objects.get(pk=created.json()["id"]).handoff_mode == "semi_auto"
    task_id = created.json()["id"]
    assert api_client.post(f"/api/v1/tasks/{task_id}/claim").status_code == 200
    patched = api_client.patch(
        f"/api/v1/tasks/{task_id}",
        {"handoff_mode": "off"},
        content_type="application/json",
    )
    assert patched.json()["handoff_mode"] == "off"
    bad = api_client.patch(
        f"/api/v1/tasks/{task_id}",
        {"handoff_mode": "full_auto"},
        content_type="application/json",
    )
    assert bad.status_code == 400


def test_auto_approves_permission_and_audits(setup):
    task, worker = setup
    enable(task)
    ingest(worker, [perm(title="Allow edit src/app.py")])
    command = replies(worker).get()
    assert command.payload == {"reply": "once"}
    assert command.request_id == "per_a"
    event = TaskEvent.objects.get(task=task, event_type="handoff.auto_answer")
    assert "approve_deny" in event.message and "per_a" in event.message and "ses_1" in event.message
    assert event.created_at is not None
    stream = WorkerEvent.objects.get(worker=worker, event_type="handoff.auto_answered")
    assert stream.payload["properties"]["taskId"] == task.pk
    assert stream.payload["properties"]["decision"] == "once"


def test_auto_approve_dispatches_in_same_sync(setup, api_client):
    task, worker = setup
    enable(task)
    body = post_sync(api_client, {"pending": {"complete": True, "permissions": [PERMISSION]}})
    assert [c["action"] for c in body.json()["commands"]] == ["permission.reply"]


@pytest.mark.parametrize(
    "record", [QUESTION, {**QUESTION, "id": "que_b", "questions": [{"text": "Name?"}]}]
)
def test_questions_always_wait_for_human(setup, record):
    task, worker = setup
    enable(task)
    ingest(worker, questions=[record])
    assert not WorkerCommand.objects.filter(worker=worker).exists()
    assert not TaskEvent.objects.filter(task=task, event_type="handoff.auto_answer").exists()
    assert WorkerEvent.objects.filter(worker=worker, event_type="handoff.escalated").exists()


@pytest.mark.parametrize(
    "title,rule",
    [
        ("Run git push --force origin main", "destructive_git"),
        ("Run git reset --hard HEAD~1", "destructive_git"),
        ("Deploy to production", "production"),
        ("rm -rf build", "destructive_delete"),
        ("Print the API key", "credentials"),
        ("Update DNS record", "dns"),
    ],
)
def test_deny_rules_escalate(setup, title, rule):
    task, worker = setup
    enable(task)
    ingest(worker, [perm(title=title)])
    assert not replies(worker).exists()
    escalated = WorkerEvent.objects.get(worker=worker, event_type="handoff.escalated")
    assert escalated.payload["properties"]["rule"] is not None
    assert handoff.matched_deny_rule({"title": title}) is not None
    assert not TaskEvent.objects.filter(task=task, event_type="handoff.auto_answer").exists()


def test_mode_off_passthrough(setup):
    task, worker = setup
    ingest(worker, [PERMISSION], [QUESTION])
    assert not WorkerCommand.objects.filter(worker=worker).exists()
    assert not WorkerEvent.objects.filter(worker=worker, event_type__startswith="handoff.").exists()


def test_unlinked_session_not_answered(setup):
    task, worker = setup
    enable(task)
    ingest(worker, [perm(sessionID="ses_other")])
    assert not replies(worker).exists()


def test_custom_deny_rules_setting(setup):
    task, worker = setup
    enable(task)
    with override_settings(HANDOFF_DENY_RULES=[("custom", r"edit")]):
        ingest(worker, [PERMISSION])
    assert not replies(worker).exists()


def test_classification():
    assert handoff.classify_prompt("permission", PERMISSION) == "approve_deny"
    assert handoff.classify_prompt("question", QUESTION) == "multiple_choice"
    assert handoff.classify_prompt("question", {"questions": [{"text": "x"}]}) == "free_text"
    assert (
        handoff.classify_prompt(
            "question", {"questions": [{"options": [{"label": "a"}]}, {"text": "x"}]}
        )
        == "free_text"
    )
    assert handoff.classify_prompt("question", {}) == "unknown"
    assert handoff.classify_prompt("question", {"questions": []}) == "unknown"
    assert handoff.classify_prompt("other", {"a": 1}) == "unknown"
    assert handoff.decide("permission", PERMISSION) == ("approve_deny", "approve", None)
    assert handoff.decide("question", QUESTION)[1] == "escalate"


def test_interaction_card_shows_prompt_type(setup):
    task, worker = setup
    ingest(worker, [PERMISSION], [QUESTION])
    from coordinator.models import PendingInteraction

    cards = {i.kind: services.interaction_card(i) for i in PendingInteraction.objects.all()}
    assert cards["permission"]["promptType"] == "approve_deny"
    assert cards["question"]["promptType"] == "multiple_choice"
