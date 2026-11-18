"""Exercise admission and overflow through the real authenticated PM endpoint."""

import httpx
import pytest

from coordinator.conversation_limits import check_context, json_bytes
from coordinator.models import Task, VoiceConversation
from coordinator.services import TaskError
from tests import test_voice
from tests.test_voice import chat, completion, tool_call

owner = test_voice.owner
voice = test_voice.voice

pytestmark = pytest.mark.django_db


def seed(grant, messages):
    VoiceConversation.objects.filter(pk=grant["id"]).update(messages=messages)


def saved(grant):
    return VoiceConversation.objects.get(pk=grant["id"])


def test_larger_history_and_reply_budget_reach_provider_without_pruning(voice, monkeypatch):
    client, grant = voice
    # Beyond BOTH previous admission limits (100 entries / 120,000 JSON chars).
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1200} for i in range(120)
    ]
    seed(grant, history)
    output = "long reply " * 3000  # exceeds the old 2,048-token output budget
    requests = []

    def provider(*args, **kwargs):
        requests.append(kwargs["json"])
        return completion(output)

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    result = chat(grant, "Continue")
    assert result.status_code == 200, result.content
    assert result.json()["text"] == output
    assert requests[0]["max_tokens"] == 8192
    assert requests[0]["messages"][1:-1] == history
    assert saved(grant).messages == [
        *history,
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": output},
    ]
    assert client.get("/api/v1/voice/status").json()["limits"]["max_messages"] == 1000


def test_message_boundary_accepts_exact_capacity_then_refuses_without_mutation(
    voice, settings, monkeypatch
):
    _, grant = voice
    settings.PM_MAX_MESSAGES = 4
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    seed(grant, history)
    requests = []

    def provider(*args, **kwargs):
        requests.append(kwargs)
        return completion("Final reply")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    assert chat(grant).status_code == 200
    previous = saved(grant).messages
    assert len(previous) == 4
    result = chat(grant)
    assert result.status_code == 409
    assert "message count limit (4)" in result.json()["detail"]
    assert len(requests) == 1
    assert saved(grant).messages == previous
    assert saved(grant).busy_until is None


def test_oversized_input_is_rejected_before_provider_and_keeps_history(
    voice, settings, monkeypatch
):
    _, grant = voice
    history = [{"role": "assistant", "content": "saved"}]
    seed(grant, history)
    settings.PM_MAX_HISTORY_BYTES = json_bytes(history) + 10

    def unexpected(*args, **kwargs):
        pytest.fail("full conversation reached provider")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", unexpected)
    response = chat(grant)
    assert response.status_code == 409
    assert "history bytes" in response.json()["detail"]
    assert saved(grant).messages == history


def test_received_reply_crossing_storage_budget_is_kept_in_full(voice, settings, monkeypatch):
    _, grant = voice
    settings.PM_MAX_HISTORY_BYTES = 200
    output = "complete reply " * 100
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request", lambda *a, **k: completion(output)
    )
    assert chat(grant).json()["text"] == output
    assert saved(grant).messages[-1]["content"] == output
    assert chat(grant).status_code == 409
    assert saved(grant).messages[-1]["content"] == output


def test_context_budget_includes_system_tools_unicode_and_output_reserve(settings):
    payload = {
        "messages": [{"role": "system", "content": "你好🌍"}],
        "tools": [{"description": "schema " * 100}],
    }
    assert json_bytes(payload) > len(str(payload))
    bound = json_bytes(payload) + 256 * 2 + settings.PM_MAX_OUTPUT_TOKENS
    settings.PM_CONTEXT_WINDOW_TOKENS = bound
    check_context(payload)
    settings.PM_CONTEXT_WINDOW_TOKENS -= 1
    with pytest.raises(TaskError, match="context token budget"):
        check_context(payload)


def test_full_context_blocks_upstream_and_preserves_history(voice, settings, monkeypatch):
    _, grant = voice
    history = [{"role": "assistant", "content": "saved"}]
    seed(grant, history)
    settings.PM_CONTEXT_WINDOW_TOKENS = settings.PM_MAX_OUTPUT_TOKENS + 1

    def unexpected(*args, **kwargs):
        pytest.fail("full context reached provider")

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", unexpected)
    result = chat(grant)
    assert result.status_code == 409
    assert "context token budget" in result.json()["detail"]
    assert saved(grant).messages == history


def test_tool_batch_capacity_is_checked_before_side_effects(voice, settings, monkeypatch):
    _, grant = voice
    settings.PM_MAX_MESSAGES = 3  # user + assistant tool call + result needs a fourth reply
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *a, **k: completion(
            calls=[tool_call("create_task", {"project": "p", "title": "No"})]
        ),
    )
    assert chat(grant).status_code == 409
    assert not Task.objects.exists()
    assert saved(grant).messages == []


def test_tool_result_crossing_budget_is_preserved_before_next_completion(
    voice, settings, monkeypatch
):
    _, grant = voice
    settings.PM_MAX_HISTORY_BYTES = 200
    requests = []

    def provider(*args, **kwargs):
        requests.append(kwargs)
        return completion(calls=[tool_call("get_overview", {})])

    monkeypatch.setattr("coordinator.voice_runtime.httpx.request", provider)
    result = chat(grant)
    assert result.status_code == 409
    assert len(requests) == 1
    assert saved(grant).messages[-1]["role"] == "tool"
    assert saved(grant).busy_until is None


def test_provider_output_limit_reports_incomplete_and_preserves_received_text(voice, monkeypatch):
    _, grant = voice
    monkeypatch.setattr(
        "coordinator.voice_runtime.httpx.request",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": "Partial reply",
                            "tool_calls": [
                                tool_call("create_task", {"project": "p", "title": "Never execute"})
                            ],
                        },
                    }
                ]
            },
        ),
    )
    result = chat(grant)
    assert result.status_code == 502
    assert "output token limit" in result.json()["detail"]
    assert not Task.objects.exists()
    assert saved(grant).messages[-1] == {"role": "assistant", "content": "Partial reply"}
    assert saved(grant).busy_until is None
