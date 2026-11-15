"""Chat downloads preserve retained history and never dereference file URLs."""

import io
import json
import zipfile

import pytest

from coordinator import services
from coordinator.models import Agent, WorkerMessage, WorkerSession

pytestmark = pytest.mark.django_db


@pytest.fixture
def conversation(agent_clients):
    worker = services.provision_worker("export-worker", Agent.objects.get(name="alpha"))
    session = WorkerSession.objects.create(
        worker=worker, remote_id="ses_export", title="México chat", window_complete=False
    )
    # Insert newest first to exercise chronological ordering instead of PK order.
    for index in reversed(range(15)):
        WorkerMessage.objects.create(
            session=session,
            remote_id=f"msg_{index:02d}",
            role="assistant" if index % 2 else "user",
            remote_time=1000 + index,
            parts=[{"type": "text", "text": f"Message {index:02d}"}],
        )
    return session


def endpoint(session, kind="md"):
    return f"/api/workers/{session.worker.name}/sessions/{session.remote_id}/export?kind={kind}"


def test_markdown_includes_all_retained_messages_in_timeline_order(api_client, conversation):
    response = api_client.get(endpoint(conversation))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/markdown; charset=utf-8"
    assert response["Cache-Control"] == "no-store, private"
    assert response["Content-Disposition"].endswith('ses_export.md"')
    text = response.content.decode()
    assert "México chat" in text
    assert "15 messages" in text
    assert "Full history is not guaranteed" in text
    assert "Supplied window complete: False" in text
    positions = [text.index(f"Message {index:02d}") for index in range(15)]
    assert positions == sorted(positions)


def test_zip_reasoning_compaction_tools_and_files(api_client, conversation):
    message = conversation.messages.get(remote_id="msg_01")
    message.content = {"summary": True}
    message.parts = [
        {"type": "reasoning", "text": "A supplied summary"},
        {"type": "tool", "tool": "read", "state": {"output": "```\nresult"}},
        {"type": "compaction", "auto": True},
        {"type": "text", "text": "Final answer"},
        {"type": "reasoning", "encrypted_content": "OPAQUE_VALUE", "text": ""},
        {
            "type": "file",
            "filename": "../../exercise.md",
            "mime": "text/markdown",
            "url": "data:text/markdown;base64,IyBFeGVyY2lzZQ==",
        },
        {"type": "file", "filename": "exercise.md", "url": "data:text/plain,second%20file"},
        {"type": "file", "filename": "local.md", "url": "file:///etc/passwd"},
        {"type": "file", "filename": "remote.md", "url": "https://example.test/private"},
        {"type": "file", "filename": "invalid.md", "url": "data:text/plain;base64,@@@"},
    ]
    message.save()
    response = api_client.get(endpoint(conversation, "zip"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert all(not name.startswith("/") and ".." not in name.split("/") for name in names)
        assert len([name for name in names if name.startswith("messages/")]) == 15
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["coverage"]["messageCount"] == 15
        assert manifest["coverage"]["fullHistoryGuaranteed"] is False
        attachments = manifest["attachments"]
        assert [item["status"] for item in attachments] == [
            "included",
            "included",
            "unavailable",
            "unavailable",
            "unavailable",
        ]
        assert archive.read(attachments[0]["path"]) == b"# Exercise"
        assert archive.read(attachments[1]["path"]) == b"second file"
        text = archive.read("conversation.md").decode()
        assert "### Compaction summary" in text
        assert text.index("A supplied summary") < text.index("### Tool: read")
        assert text.index("### Tool: read") < text.index("### Compaction marker")
        assert text.index("### Compaction marker") < text.index("Final answer")
        assert "no readable summary was supplied" in text
        assert "OPAQUE_VALUE" not in text
        assert "````\n" in text  # Tool output cannot break its enclosing fence.
        assert f"]({attachments[0]['path']})" in text
        per_message = archive.read("messages/0002-msg_01.md").decode()
        assert f"](../{attachments[0]['path']})" in per_message
        records = json.loads(archive.read("messages.json"))
        assert records[1]["parts"] == message.parts


def test_attachment_limits_are_explicit(api_client, conversation, monkeypatch):
    monkeypatch.setattr("coordinator.chat_export.MAX_TOTAL_ATTACHMENT_BYTES", 2)
    message = conversation.messages.first()
    message.parts = [{"type": "file", "url": "data:text/plain,too%20large"}]
    message.save()
    with zipfile.ZipFile(io.BytesIO(api_client.get(endpoint(conversation, "zip")).content)) as z:
        manifest = json.loads(z.read("manifest.json"))
        attachment = manifest["attachments"][0]
        assert attachment["status"] == "unavailable"
        assert "limit" in attachment["reason"]
        assert not any(name.startswith("attachments/") for name in z.namelist())


def test_exports_scope_auth_format_and_unknown_session(
    api_client, client, agent_clients, conversation
):
    assert client.get(endpoint(conversation)).status_code == 401
    beta = agent_clients[1]
    assert beta.get(endpoint(conversation)).status_code == 403
    assert api_client.get(endpoint(conversation, "exe")).status_code == 400
    url = endpoint(conversation).replace("ses_export/export", "missing/export")
    assert api_client.get(url).status_code == 404
    # Staff use the same scoped download route, including offline observations.
    from django.contrib.auth.models import User

    user = User.objects.create_user("export-staff", is_staff=True)
    client.force_login(user)
    assert client.get(endpoint(conversation, "zip")).status_code == 200


def test_empty_session_download(api_client, conversation):
    conversation.messages.all().delete()
    response = api_client.get(endpoint(conversation))
    assert response.status_code == 200
    assert "No retained messages" in response.content.decode()
