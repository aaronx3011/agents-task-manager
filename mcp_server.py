"""Stdio MCP tools backed by the authenticated coordinator REST API."""

import os
from typing import Literal
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
mcp = FastMCP("task-coordinator")


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
        try:
            detail = exc.response.json().get("detail", "Request rejected")
        except (ValueError, AttributeError):
            detail = "Request rejected"
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
) -> dict:
    """Create a task with acceptance criteria and optional existing prerequisite IDs."""
    return call(
        "POST",
        "tasks",
        json={
            "title": title,
            "project": project,
            "description": description,
            "priority": priority,
            "depends_on": depends_on or [],
        },
    )


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


if __name__ == "__main__":
    mcp.run()
