"""Chat room tool privileges: one actor writes, everyone else only reads (GH #149)."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from accounts.models import UserProfile
from coordinator import chat_policy
from coordinator.models import (
    Agent,
    ChatMessage,
    ChatParticipant,
    ChatToolResult,
    Task,
    Worker,
    WorkerMessage,
    WorkerSession,
)

from .test_chat_rooms import (
    MODEL,
    add_participant,
    advance,
    completion,
    make_room,
    post,
    stub,
    tool_call,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner(db):
    user = get_user_model().objects.create_user("room-owner", password="test-password")
    UserProfile.objects.update_or_create(user=user, defaults={"role": "owner"})
    return user


@pytest.fixture
def rooms(owner, project, settings):
    settings.FIREWORKS_API_KEY = "fireworks-server-secret"
    settings.CHAT_ROOM_MODELS = (MODEL,)
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def session(db):
    agent = Agent(name="session-agent")
    agent.set_new_api_key()
    agent.save()
    worker = Worker.objects.create(
        name="box",
        agent=agent,
        connected=True,
        stream_connected=True,
        last_sync_at=timezone.now(),
        directory="/srv/app",
    )
    worker_session = WorkerSession.objects.create(
        worker=worker,
        remote_id="ses_fixture",
        title="Refactor the queue",
        directory="/srv/app",
        model={"providerID": "anthropic", "modelID": "claude-sonnet-4-5"},
    )
    for index, (role, text) in enumerate(
        [("user", "Refactor it"), ("assistant", "Done, with tests")], start=1
    ):
        WorkerMessage.objects.create(
            session=worker_session,
            remote_id=f"msg_{index}",
            role=role,
            content={"text": text},
            parts=[{"type": "text", "text": text}],
            remote_time=index,
        )
    return worker_session


def test_schema_split_gives_mutating_tools_only_to_the_actor():
    actor = ChatParticipant(role="Lead", can_act=True)
    reader = ChatParticipant(role="Critic", can_act=False)
    reader_names = {item["function"]["name"] for item in chat_policy.tool_configs(reader)}
    actor_names = {item["function"]["name"] for item in chat_policy.tool_configs(actor)}
    assert not reader_names & set(chat_policy.ACT_TOOLS)
    assert set(chat_policy.ACT_TOOLS) <= actor_names
    assert set(chat_policy.READ_TOOLS) <= reader_names
    assert {"list_sessions", "get_session", "read_session_messages"} <= reader_names
    # No tool schema may ever advertise an authentication field.
    for config in chat_policy.tool_configs(actor):
        properties = config["function"]["parameters"]["properties"]
        assert not {"conversation_id", "token", "api_key"} & set(properties)


def test_the_acting_participant_creates_a_task_audited_as_the_owner(
    rooms, project, owner, monkeypatch
):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Lead", can_act=True)
    stub(
        monkeypatch,
        [
            completion(
                calls=[tool_call("create_task", {"project": project.slug, "title": "Real work"})]
            ),
            completion("I filed it."),
        ],
    )
    post(rooms, room["id"], "File the work")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    assert response.json()["message"]["toolEvents"] == [{"tool": "create_task", "is_error": False}]
    task = Task.objects.get(title="Real work")
    # Audited as the signed-in owner, never as the model or an agent key.
    assert task.events.get().actor_user == owner
    assert ChatToolResult.objects.get().tool == "create_task"


def test_a_read_only_participant_cannot_mutate_even_by_fabricating_a_call(
    rooms, project, monkeypatch
):
    room = make_room(rooms, project)
    # No participant may act in this room at all.
    add_participant(rooms, room["id"], "Critic", can_act=False)
    stub(
        monkeypatch,
        [
            completion(
                calls=[tool_call("create_task", {"project": project.slug, "title": "Sneaky"})]
            ),
            completion("I could not do that."),
        ],
    )
    post(rooms, room["id"], "Create a task")
    response = advance(rooms, room["id"])
    # PermissionDenied propagates rather than becoming a tool result.
    assert response.status_code == 403, response.content
    assert "read-only" in str(response.content)
    assert not Task.objects.filter(title="Sneaky").exists()
    assert not ChatToolResult.objects.exists()


def test_a_read_only_participant_in_a_room_with_an_actor_still_cannot_mutate(
    rooms, project, monkeypatch
):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Lead", position=1, can_act=True)
    add_participant(rooms, room["id"], "Critic", position=2)
    stub(
        monkeypatch,
        [
            completion("Lead speaks first."),
            completion(calls=[tool_call("update_task_status", {"id": 1, "status": "done"})]),
        ],
    )
    post(rooms, room["id"], "Go")
    assert advance(rooms, room["id"]).status_code == 200
    blocked = advance(rooms, room["id"])
    assert blocked.status_code == 403
    assert not ChatToolResult.objects.exists()


def test_read_tools_work_for_a_read_only_participant(rooms, project, monkeypatch):
    task = Task.objects.create(project=project, title="Visible task")
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    stub(
        monkeypatch,
        [
            completion(calls=[tool_call("get_task", {"id": task.pk})]),
            completion("That task is pending."),
        ],
    )
    post(rooms, room["id"], "What is the status?")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    assert response.json()["message"]["toolEvents"] == [{"tool": "get_task", "is_error": False}]
    assert response.json()["message"]["text"] == "That task is pending."
    # Reads are not written to the idempotency ledger as replayable writes.
    assert ChatToolResult.objects.get().tool == "get_task"


def test_a_participant_can_read_stored_worker_sessions(rooms, project, session, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    requests = stub(
        monkeypatch,
        [
            completion(calls=[tool_call("list_sessions", {"worker": session.worker.name})]),
            completion(
                calls=[
                    tool_call(
                        "read_session_messages",
                        {"worker": session.worker.name, "session_id": session.remote_id},
                        call_id="call_two",
                    )
                ]
            ),
            completion("That session refactored the queue and added tests."),
        ],
    )
    post(rooms, room["id"], "What happened in that session?")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    assert [event["tool"] for event in response.json()["message"]["toolEvents"]] == [
        "list_sessions",
        "read_session_messages",
    ]
    assert all(event["is_error"] is False for event in response.json()["message"]["toolEvents"])
    # The transcript the model actually received contains the stored session text.
    body = str(requests[-1][1]["json"]["messages"])
    assert "Refactor the queue" in body
    assert "Done, with tests" in body


def test_get_session_requires_its_identifiers(rooms, project, session, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    stub(
        monkeypatch,
        [
            completion(calls=[tool_call("get_session", {"worker": session.worker.name})]),
            completion("I could not read that session."),
        ],
    )
    post(rooms, room["id"], "Read it")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    # A missing required argument becomes a tool error, not a fabricated success.
    assert response.json()["message"]["toolEvents"] == [{"tool": "get_session", "is_error": True}]


def test_unsupported_tool_parameters_are_refused(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    stub(
        monkeypatch,
        [
            completion(calls=[tool_call("list_sessions", {"not_a_field": "x"})]),
            completion("That did not work."),
        ],
    )
    post(rooms, room["id"], "Go")
    response = advance(rooms, room["id"])
    assert response.status_code == 200
    assert response.json()["message"]["toolEvents"] == [{"tool": "list_sessions", "is_error": True}]


def test_a_repeated_write_is_replayed_and_creates_one_task(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Lead", can_act=True)
    call = tool_call("create_task", {"project": project.slug, "title": "Once only"})
    stub(
        monkeypatch,
        [
            completion(calls=[call]),
            completion("Filed."),
            completion(calls=[call]),
            completion("Already filed."),
        ],
    )
    post(rooms, room["id"], "File it")
    assert advance(rooms, room["id"]).status_code == 200
    post(rooms, room["id"], "File it again")
    assert advance(rooms, room["id"]).status_code == 200
    assert Task.objects.filter(title="Once only").count() == 1
    assert ChatToolResult.objects.filter(tool="create_task").count() == 1


def test_a_failing_tool_becomes_a_tool_result_not_a_fabricated_success(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Lead", can_act=True)
    stub(
        monkeypatch,
        [
            completion(calls=[tool_call("get_task", {"id": 999999})]),
            completion("I could not find that task."),
        ],
    )
    post(rooms, room["id"], "Read task 999999")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    assert response.json()["message"]["toolEvents"] == [{"tool": "get_task", "is_error": True}]


def test_the_tool_round_cap_stops_a_runaway_turn(rooms, project, monkeypatch, settings):
    settings.CHAT_ROOM_MAX_TOOL_ROUNDS = 2
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    stub(monkeypatch, [completion(calls=[tool_call("list_projects", {})])] * 6)
    post(rooms, room["id"], "Loop forever")
    response = advance(rooms, room["id"])
    assert response.status_code == 200, response.content
    assert "stopped after 2 tool rounds" in response.json()["message"]["text"]
    # The turn still completed and was popped, so the room is not wedged.
    assert response.json()["room"]["pendingSpeakers"] == []
    assert ChatMessage.objects.filter(author_kind="participant").count() == 1


def test_an_unknown_tool_name_is_refused(rooms, project, monkeypatch):
    room = make_room(rooms, project)
    add_participant(rooms, room["id"], "Critic")
    stub(
        monkeypatch,
        [
            completion(calls=[tool_call("delete_everything", {})]),
            completion("No such tool."),
        ],
    )
    post(rooms, room["id"], "Go")
    response = advance(rooms, room["id"])
    assert response.status_code == 200
    assert response.json()["message"]["toolEvents"] == [
        {"tool": "delete_everything", "is_error": True}
    ]
