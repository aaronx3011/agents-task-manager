import json as jsonlib
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import anyio
import httpx
import pytest
from django.test import Client
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
                "handoff_mode": "off",
            },
        ),
        (
            lambda: tools.create_task("Spec", "design", handoff_mode="semi_auto"),
            "POST",
            "/tasks",
            {
                "title": "Spec",
                "project": "design",
                "description": "",
                "priority": 100,
                "depends_on": [],
                "handoff_mode": "semi_auto",
            },
        ),
        (
            lambda: tools.update_task(7, handoff_mode="semi_auto"),
            "PATCH",
            "/tasks/7",
            {"handoff_mode": "semi_auto"},
        ),
        (
            lambda: tools.update_task(7, title="New", priority=0, depends_on=[]),
            "PATCH",
            "/tasks/7",
            {"title": "New", "priority": 0, "depends_on": []},
        ),
        (lambda: tools.list_pending_prompts(), "GET", "/interactions", None),
        (
            lambda: tools.list_pipes("design", limit=5, offset=10),
            "GET",
            "/projects/design/pipes",
            None,
        ),
        (lambda: tools.get_pipe("design", 9), "GET", "/projects/design/pipes/9", None),
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


def test_pipe_reads_forward_pagination(requests):
    tools.list_pipes("design", limit=5, offset=10)
    assert dict(requests[0].url.params) == {"limit": "5", "offset": "10"}
    tools.list_pipes("design")
    assert dict(requests[1].url.params) == {"limit": "20", "offset": "0"}


def test_update_task_requires_a_field_and_never_sends_an_empty_patch(requests):
    with pytest.raises(ValueError, match="at least one field"):
        tools.update_task(7)
    assert requests == []


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


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"detail": "Only the claiming agent may change this task"}, "Only the claiming agent"),
        ({"handoff_mode": ['"full_auto" is not a valid choice.']}, 'handoff_mode: "full_auto"'),
        ({"depends_on": {"0": ["Invalid pk"]}}, "depends_on: 0: Invalid pk"),
        (["Use lifecycle actions for protected task fields"], "Use lifecycle actions"),
        ("<html>gateway</html>", "Request rejected"),
    ],
)
def test_field_validation_errors_reach_the_agent(monkeypatch, requests, body, expected):
    def reject(method, url, **kwargs):
        request = httpx.Request(method, url)
        if isinstance(body, str):
            return httpx.Response(400, text=body, request=request)
        return httpx.Response(400, json=body, request=request)

    monkeypatch.setattr(httpx, "request", reject)
    with pytest.raises(RuntimeError) as error:
        tools.update_task(7, handoff_mode="semi_auto")
    assert expected in str(error.value)
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


@pytest.fixture
def coordinator(monkeypatch, agent_clients):
    """Run the MCP tools against the real Django API instead of a canned response.

    Hand-off mode is an end-to-end contract: ownership (403), the choice list
    (400) and persistence are enforced by the service layer, not the adapter.
    """
    monkeypatch.setenv("COORDINATOR_URL", "http://coordinator.test")
    keys = [client.defaults["HTTP_AUTHORIZATION"].split()[1] for client in agent_clients]
    http = Client()

    def dispatch(method, url, *, headers, json=None, params=None, **kwargs):
        path = httpx.URL(url).path
        if params:
            path = f"{path}?{urlencode(params, doseq=True)}"
        response = http.generic(
            method,
            path,
            b"" if json is None else jsonlib.dumps(json).encode(),
            content_type="application/json",
            HTTP_AUTHORIZATION=headers["Authorization"],
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.get("Content-Type", "application/json")},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(httpx, "request", dispatch)

    def as_agent(index):
        monkeypatch.setenv("COORDINATOR_API_KEY", keys[index])

    as_agent(0)
    return as_agent


@pytest.mark.django_db
def test_handoff_mode_round_trips_through_the_mcp_tools(coordinator, project):
    from coordinator.models import Task

    default = tools.create_task("Default", "coordinator")
    assert default["handoff_mode"] == "off"
    created = tools.create_task("Hand-off", "coordinator", handoff_mode="semi_auto")
    assert created["handoff_mode"] == "semi_auto"
    # Reload from the database, not the response the adapter just echoed back.
    assert Task.objects.get(pk=created["id"]).handoff_mode == "semi_auto"
    assert tools.get_task(created["id"])["handoff_mode"] == "semi_auto"
    listed = {task["id"]: task["handoff_mode"] for task in tools.list_tasks("coordinator")["items"]}
    assert listed == {default["id"]: "off", created["id"]: "semi_auto"}

    tools.claim_task(created["id"])
    assert tools.update_task(created["id"], handoff_mode="off")["handoff_mode"] == "off"
    assert Task.objects.get(pk=created["id"]).handoff_mode == "off"
    assert tools.update_task(created["id"], handoff_mode="semi_auto")["handoff_mode"] == "semi_auto"


@pytest.mark.django_db
def test_update_task_rejects_other_agents_tasks_and_unknown_modes(coordinator, project):
    from coordinator.models import Task

    task = tools.create_task("Hand-off", "coordinator", handoff_mode="semi_auto")
    tools.claim_task(task["id"])

    with pytest.raises(RuntimeError, match="HTTP 400") as invalid:
        tools.update_task(task["id"], handoff_mode="full_auto")
    assert "handoff_mode" in str(invalid.value)

    coordinator(1)
    with pytest.raises(RuntimeError, match="HTTP 403") as forbidden:
        tools.update_task(task["id"], handoff_mode="off")
    assert "claiming agent" in str(forbidden.value)
    assert Task.objects.get(pk=task["id"]).handoff_mode == "semi_auto"


@pytest.mark.django_db
def test_list_pending_prompts_reports_the_prompt_type(coordinator):
    from django.utils import timezone

    from coordinator import services
    from coordinator.models import Agent

    from .test_worker_pending import PERMISSION, QUESTION

    worker = services.provision_worker("worker-a", Agent.objects.get(name="alpha"))
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.save()
    services.store_sessions(worker, [{"id": "ses_1", "directory": "/repo", "title": "T1"}])
    services.ingest_pending_interactions(
        worker, {"complete": True, "permissions": [PERMISSION], "questions": [QUESTION]}
    )

    prompts = tools.list_pending_prompts()
    assert prompts["count"] == 2
    assert {prompt["id"]: prompt["promptType"] for prompt in prompts["items"]} == {
        "per_a": "approve_deny",
        "que_a": "multiple_choice",
    }
    assert {prompt["worker"] for prompt in prompts["items"]} == {"worker-a"}

    # An agent with no bound worker sees nothing, so prompts never leak sideways.
    coordinator(1)
    assert tools.list_pending_prompts() == {"count": 0, "items": []}


@pytest.mark.django_db
def test_pipe_reads_expose_stages_and_refuse_unknown_or_foreign_pipes(coordinator, project):
    from django.contrib.auth.models import User
    from django.test import Client
    from django.utils import timezone

    from coordinator import services
    from coordinator.models import Agent, Pipe, Project, ProjectPlan

    model = {"providerID": "test", "modelID": "pipe-model"}
    worker = services.provision_worker("mcp-worker", Agent.objects.get(name="alpha"))
    worker.connected = worker.stream_connected = True
    worker.last_sync_at = timezone.now()
    worker.default_model = model
    worker.model_catalog = [model]
    worker.agent_catalog = [{"name": "build", "mode": "primary"}]
    worker.directory = "/workspace"
    worker.save()
    plan = ProjectPlan.objects.create(
        project=project, default_worker=worker, directory="/workspace"
    )

    staff = Client()
    staff.force_login(User.objects.create_user("pipe-planner", is_staff=True))
    response = staff.post(
        f"/api/projects/{plan.project.slug}/planning",
        {
            "revision": 0,
            "operation": "pipe_create",
            "pipe": {
                "title": "MCP pipe",
                "stages": [
                    {
                        "name": "Plan",
                        "role": "plan",
                        "worker": "mcp-worker",
                        "model": model,
                    }
                ],
            },
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    pipe = Pipe.objects.get()
    stage = pipe.stages.get()
    stage.artifact = "PLAN ARTIFACT"
    stage.save(update_fields=["artifact"])

    listed = tools.list_pipes("coordinator")
    assert listed["project"] == "coordinator" and listed["count"] == 1
    row = listed["pipes"][0]
    assert row["id"] == pipe.pk and row["status"] == "running"
    assert row["stages"][0]["name"] == "Plan"
    assert row["stages"][0]["status"] == "queued"
    assert row["stages"][0]["worker"] == "mcp-worker"
    assert row["stages"][0]["model"] == model
    assert row["stages"][0]["artifact"] == "PLAN ARTIFACT"

    one = tools.get_pipe("coordinator", pipe.pk)
    assert one["project"] == "coordinator" and one["pipe"]["id"] == pipe.pk

    with pytest.raises(RuntimeError, match="HTTP 404"):
        tools.get_pipe("coordinator", pipe.pk + 1)
    foreign = Project.objects.create(slug="foreign", name="Foreign")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        tools.get_pipe(foreign.slug, pipe.pk)
    empty = tools.list_pipes("foreign")
    assert empty["count"] == 0 and empty["pipes"] == []
    with pytest.raises(RuntimeError, match="HTTP 404"):
        tools.list_pipes("missing")


def handoff_choices(schema):
    """The enum of a handoff_mode property, whether or not it is wrapped in anyOf."""
    if "enum" in schema:
        return sorted(schema["enum"])
    branches = [branch for branch in schema.get("anyOf", []) if "enum" in branch]
    assert len(branches) == 1, schema
    return sorted(branches[0]["enum"])


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
                        "update_task",
                        "list_pending_prompts",
                        "list_pipes",
                        "get_pipe",
                        "claim_next_task",
                        "claim_task",
                        "heartbeat",
                        "update_status",
                        "release_task",
                        "add_note",
                        "report_planning_job",
                    }
                    schemas = {tool.name: tool.inputSchema for tool in result.tools}
                    # Clients must see the hand-off choices and that only task_id
                    # is required, or they cannot form a valid update_task call.
                    update = schemas["update_task"]
                    assert update["required"] == ["task_id"]
                    assert handoff_choices(update["properties"]["handoff_mode"]) == [
                        "off",
                        "semi_auto",
                    ]
                    created = schemas["create_task"]["properties"]["handoff_mode"]
                    assert handoff_choices(created) == ["off", "semi_auto"]
                    assert created.get("default") == "off"
                    assert schemas["list_pending_prompts"].get("properties", {}) == {}
                    # Pipe tools are read-only: no create/approve/stop schema exists.
                    assert schemas["list_pipes"]["required"] == ["project"]
                    assert schemas["get_pipe"]["required"] == ["project", "pipe_id"]
                    assert schemas["list_pipes"]["properties"]["limit"].get("default") == 20

                    failure = await session.call_tool("get_overview", {})
                    assert failure.isError
                    assert "COORDINATOR_API_KEY" in failure.content[0].text

    anyio.run(verify)
