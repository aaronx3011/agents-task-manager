import os
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import mcp_server as tools


@pytest.fixture
def requests(monkeypatch):
    captured = []
    monkeypatch.setenv("COORDINATOR_URL", "http://coordinator.test:8000/")
    monkeypatch.setenv("COORDINATOR_API_KEY", "test-agent-key")

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(httpx, "request", client.request)
        yield captured


@pytest.mark.parametrize(
    "invoke,method,path,body",
    [
        (lambda: tools.get_overview(), "GET", "/overview", None),
        (lambda: tools.get_task(7), "GET", "/tasks/7", None),
        (
            lambda: tools.create_task("Spec", "design", "Acceptance", 5, [3]),
            "POST",
            "/tasks",
            {
                "title": "Spec",
                "project": "design",
                "description": "Acceptance",
                "priority": 5,
                "depends_on": [3],
            },
        ),
        (lambda: tools.claim_next_task("design"), "POST", "/tasks/next", {"project": "design"}),
        (lambda: tools.claim_task(7), "POST", "/tasks/7/claim", {}),
        (lambda: tools.heartbeat(7), "POST", "/tasks/7/heartbeat", {}),
        (
            lambda: tools.report_planning_job(7, "job-token", "done", "Verified"),
            "POST",
            "/planning/jobs/7/report",
            {"token": "job-token", "status": "done", "summary": "Verified"},
        ),
        (
            lambda: tools.update_status(7, "blocked", "Approval"),
            "POST",
            "/tasks/7/status",
            {"status": "blocked", "reason": "Approval", "result_summary": ""},
        ),
        (lambda: tools.release_task(7, "Pause"), "POST", "/tasks/7/release", {"reason": "Pause"}),
        (
            lambda: tools.add_note(7, "Artifact URL"),
            "POST",
            "/tasks/7/notes",
            {"message": "Artifact URL"},
        ),
    ],
)
def test_tools_call_authenticated_http_api(requests, invoke, method, path, body):
    import json

    assert invoke() == {"ok": True}
    request = requests[0]
    assert request.method == method
    assert request.url.path == f"/api{path}"
    assert request.headers["Authorization"] == "Bearer test-agent-key"
    assert (json.loads(request.content) if request.content else None) == body


def test_list_filters_and_pagination_are_preserved(requests):
    tools.list_tasks("design", "pending", True, "alpha", limit=5, offset=10)
    assert dict(requests[0].url.params) == {
        "project": "design",
        "status": "pending",
        "ready": "true",
        "claimed_by": "alpha",
        "limit": "5",
        "offset": "10",
    }


def test_http_failure_is_surfaced_without_credentials(monkeypatch, requests):
    def reject(method, url, **kwargs):
        return httpx.Response(
            403,
            json={"detail": "Only the claiming agent may change this task"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", reject)
    with pytest.raises(RuntimeError, match="Coordinator HTTP 403") as error:
        tools.claim_task(7)
    assert "test-agent-key" not in str(error.value)


def test_transport_failure_does_not_silently_retry(monkeypatch, requests):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise httpx.ReadTimeout("Network interrupted")

    monkeypatch.setattr(httpx, "request", timeout)
    with pytest.raises(RuntimeError, match="task state before retrying"):
        tools.claim_next_task()
    assert len(calls) == 1


def test_missing_credentials_fail_before_request(monkeypatch):
    monkeypatch.setenv("COORDINATOR_URL", "http://localhost:8000")
    monkeypatch.delenv("COORDINATOR_API_KEY", raising=False)
    with pytest.raises(ValueError, match="COORDINATOR_API_KEY"):
        tools.get_overview()


def test_no_ready_task_is_distinguished_from_other_not_found_errors(monkeypatch, requests):
    def no_ready(method, url, **kwargs):
        return httpx.Response(
            404, json={"detail": "No ready tasks."}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx, "request", no_ready)
    assert tools.claim_next_task() is None
    with pytest.raises(RuntimeError, match="HTTP 404"):
        tools.get_task(7)


def test_stdio_protocol_registers_all_tools():
    async def verify():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(tools.__file__).resolve())],
            env={**os.environ, "COORDINATOR_API_KEY": ""},
        )
        with anyio.fail_after(20):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    assert {tool.name for tool in result.tools} == {
                        "get_overview",
                        "list_tasks",
                        "get_task",
                        "create_task",
                        "claim_next_task",
                        "claim_task",
                        "heartbeat",
                        "update_status",
                        "release_task",
                        "add_note",
                        "report_planning_job",
                    }
                    failure = await session.call_tool("get_overview", {})
                    assert failure.isError
                    assert "COORDINATOR_API_KEY" in failure.content[0].text

    anyio.run(verify)
