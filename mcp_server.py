"""Stdio MCP tools backed by the authenticated coordinator REST API."""

import os
from typing import Literal
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
mcp = FastMCP("task-coordinator")


def error_messages(value) -> str:
    """Flatten a DRF error value, which nests lists and dicts per field."""
    if isinstance(value, list):
        return " ".join(error_messages(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{name}: {error_messages(item)}" for name, item in sorted(value.items()))
    return str(value)


def error_detail(response) -> str:
    """Readable rejection text, including per-field validation errors (400s).

    Only the response body is used, so the agent's API key is never echoed back.
    """
    try:
        body = response.json()
    except ValueError:
        return "Request rejected"
    if isinstance(body, dict) and "detail" in body:
        body = body["detail"]
    return error_messages(body).strip()[:500] or "Request rejected"


def call(method: str, path: str, *, empty_claim_ok=False, **kwargs):
    base_url = os.environ.get("COORDINATOR_URL", "http://localhost:8000").rstrip("/")
    key = os.environ.get("COORDINATOR_API_KEY", "")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("COORDINATOR_URL must be an absolute HTTP(S) service URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("COORDINATOR_URL must be the service origin, without /api or credentials")
    if not key:
        raise ValueError("Set COORDINATOR_API_KEY to this agent's API key")
    try:
        response = httpx.request(
            method,
            f"{base_url}/api/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
            **kwargs,
        )
        if empty_claim_ok and response.status_code == 404:
            if response.json() == {"detail": "No ready tasks."}:
                return None
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = error_detail(exc.response)
        raise RuntimeError(f"Coordinator HTTP {exc.response.status_code}: {detail}") from None
    except httpx.RequestError:
        raise RuntimeError(
            "Coordinator request failed. Check service connectivity and task state before retrying."
        ) from None
    return response.json()


@mcp.tool()
def get_overview() -> list[dict]:
    """Summarize project status counts, active claims, blockers and dependency waiters."""
    return call("GET", "overview")


@mcp.tool()
def list_tasks(
    project: str | None = None,
    status: Literal["pending", "in_progress", "blocked", "review", "done", "cancelled"]
    | None = None,
    ready_only: bool = False,
    claimed_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List tasks; returns count and a page of items. Use offset to retrieve further pages."""
    params = {"limit": limit, "offset": offset}
    if project is not None:
        params["project"] = project
    if status is not None:
        params["status"] = status
    if claimed_by is not None:
        params["claimed_by"] = claimed_by
    if ready_only:
        params["ready"] = "true"
    return call("GET", "tasks", params=params)


@mcp.tool()
def get_task(task_id: int) -> dict:
    """Get task details, prerequisites and the most recent twenty audit events."""
    return call("GET", f"tasks/{task_id}")


@mcp.tool()
def create_task(
    title: str,
    project: str,
    description: str = "",
    priority: int = 100,
    depends_on: list[int] | None = None,
    handoff_mode: Literal["off", "semi_auto"] = "off",
) -> dict:
    """Create a task with acceptance criteria and optional existing prerequisite IDs.

    handoff_mode "semi_auto" lets this task's sessions auto-approve binary
    approve/deny permission prompts unless a deny rule matches; questions and
    denied patterns still wait for a human. "off" is the default behaviour.
    """
    return call(
        "POST",
        "tasks",
        json={
            "title": title,
            "project": project,
            "description": description,
            "priority": priority,
            "depends_on": depends_on or [],
            "handoff_mode": handoff_mode,
        },
    )


@mcp.tool()
def update_task(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    project: str | None = None,
    priority: int | None = None,
    depends_on: list[int] | None = None,
    handoff_mode: Literal["off", "semi_auto"] | None = None,
) -> dict:
    """Edit your claimed task's editable fields; omitted arguments are left unchanged.

    Only the claiming agent may edit a task, so another agent's task is rejected
    with HTTP 403. Use the lifecycle tools for status, blockers and summaries.
    """
    changes = {
        "title": title,
        "description": description,
        "project": project,
        "priority": priority,
        "depends_on": depends_on,
        "handoff_mode": handoff_mode,
    }
    changes = {name: value for name, value in changes.items() if value is not None}
    if not changes:
        raise ValueError("Pass at least one field to update")
    return call("PATCH", f"tasks/{task_id}", json=changes)


@mcp.tool()
def list_pending_prompts() -> dict:
    """List unresolved permission and question prompts for your bound worker.

    Read-only: each card carries promptType (approve_deny, multiple_choice,
    free_text, unknown) alongside worker, session and request identity. Answering
    prompts stays with humans through the coordinator workspace.
    """
    return call("GET", "interactions")


@mcp.tool()
def claim_next_task(project: str | None = None) -> dict | None:
    """Atomically claim the highest-priority ready task; null means no ready task."""
    return call("POST", "tasks/next", json={"project": project}, empty_claim_ok=True)


@mcp.tool()
def claim_task(task_id: int) -> dict:
    """Claim a specific pending task only if all prerequisites are done and it is unclaimed."""
    return call("POST", f"tasks/{task_id}/claim", json={})


@mcp.tool()
def heartbeat(task_id: int) -> dict:
    """Renew your in-progress claim. Call every 2–5 minutes; claims expire after 15 minutes."""
    return call("POST", f"tasks/{task_id}/heartbeat", json={})


@mcp.tool()
def update_status(
    task_id: int,
    status: Literal["in_progress", "blocked", "review", "done", "cancelled"],
    reason: str = "",
    result_summary: str = "",
) -> dict:
    """Update your task. Blocked needs reason; review/done need summary. Resume via in_progress."""
    return call(
        "POST",
        f"tasks/{task_id}/status",
        json={
            "status": status,
            "reason": reason,
            "result_summary": result_summary,
        },
    )


@mcp.tool()
def release_task(task_id: int, reason: str = "") -> dict:
    """Return your task to pending and clear its claim. Include progress and remaining work."""
    return call("POST", f"tasks/{task_id}/release", json={"reason": reason})


@mcp.tool()
def add_note(task_id: int, message: str) -> dict:
    """Append a durable note. A task claimed by another agent cannot be annotated."""
    return call("POST", f"tasks/{task_id}/notes", json={"message": message})


@mcp.tool()
def list_pipes(project: str, limit: int = 20, offset: int = 0) -> dict:
    """List a project's pipes newest first, with stages, workers, models, status and artifacts.

    Read-only: pipe create/approve/stop remain staff-session-only because they
    control worker execution and agent keys hold no owner/CSRF queue capability.
    """
    return call(
        "GET",
        f"projects/{project}/pipes",
        params={"limit": limit, "offset": offset},
    )


@mcp.tool()
def get_pipe(project: str, pipe_id: int) -> dict:
    """Read one pipe on a project: ordered stages, status, artifacts and latest jobs.

    Read-only. A pipe from another project is rejected with HTTP 404.
    """
    return call("GET", f"projects/{project}/pipes/{pipe_id}")


@mcp.tool()
def report_planning_job(
    job_id: int,
    token: str,
    status: Literal["heartbeat", "done", "blocked", "review"],
    summary: str = "",
) -> dict:
    """Report a planning job using its first-prompt capability. Done requires verified results.

    Use heartbeat every 2–5 minutes. The token permits reporting only this job,
    even when the local MCP agent differs from the worker's task owner.
    """
    return call(
        "POST",
        f"planning/jobs/{job_id}/report",
        json={
            "token": token,
            "status": status,
            "summary": summary,
        },
    )


if __name__ == "__main__":
    mcp.run()
