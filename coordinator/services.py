import hashlib
import json
import secrets
import uuid
from urllib.parse import urlsplit, urlunsplit

from django.apps import apps
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import BigIntegerField, F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import handoff, sync
from .dbretry import retry_sqlite
from .errors import TaskError
from .models import (
    Agent,
    PendingInteraction,
    Pipe,
    PipeStage,
    PlanningCard,
    PlanningJob,
    Project,
    ProjectPlan,
    ServerState,
    Task,
    TaskEvent,
    TaskExecutionAttempt,
    Worker,
    WorkerCommand,
    WorkerEvent,
    WorkerMessage,
    WorkerSession,
)
from .realtime import dispatch_notification

__all__ = ["TaskError"]


@retry_sqlite
def register_agent(name):
    """Explicit human registration. Return a raw credential only to this caller."""
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise TaskError("Agent name must contain 1–100 characters", 400)
    name = name.strip()
    if Agent.objects.filter(name=name).exists():
        raise TaskError("An agent with this name already exists", 409)
    agent = Agent(name=name)
    raw_key = agent.set_new_api_key()
    try:
        with transaction.atomic():
            agent.save(force_insert=True)
    except IntegrityError:
        # The unique constraint is authoritative when two staff requests race.
        if Agent.objects.filter(name=name).exists():
            raise TaskError("An agent with this name already exists", 409) from None
        raise
    return agent, raw_key


def agent_registration_card(agent):
    return {
        "id": agent.pk,
        "name": agent.name,
        "is_active": agent.is_active,
        "last_seen": agent.last_seen,
        "created_at": agent.created_at,
    }


@retry_sqlite
def create_project(slug, name, kind, repo_url="", docs_path=""):
    """Explicit dashboard creation; the database arbitrates competing slugs."""
    try:
        with transaction.atomic():
            project = Project.objects.create(
                slug=slug, name=name, kind=kind, repo_url=repo_url, docs_path=docs_path
            )
            ProjectPlan.objects.create(project=project)
            transaction.on_commit(
                lambda: dispatch_notification("project.created", subject="projects"), robust=True
            )
    except IntegrityError:
        if Project.objects.filter(slug=slug).exists():
            raise TaskError(
                "A project with this slug already exists. Choose another slug.", 409
            ) from None
        raise
    return project


def locked(queryset, skip_locked=False):
    if connection.features.has_select_for_update:
        return queryset.select_for_update(skip_locked=skip_locked)
    return queryset


def get_locked(task_id, skip_locked=False):
    task = locked(Task.objects.filter(pk=task_id), skip_locked=skip_locked).first()
    if task is not None:
        return task
    if skip_locked and Task.objects.filter(pk=task_id).exists():
        raise TaskError("Task is currently locked; retry later")
    raise TaskError("Task not found", 404)


def require_owner(task, agent, human_actor=None):
    if human_actor is not None and agent is None:
        return
    if agent is None or task.claimed_by_id != agent.pk:
        raise TaskError("Only the claiming agent may change this task", 403)


def audit_message(message, human_actor=None):
    return f"Human staff {human_actor}: {message}" if human_actor is not None else message


def human_user(username):
    """The account behind a human actor label, resolved inside the caller's transaction (#44).

    Labels come from request.user at request time, so a missing match only happens
    for history written before accounts existed; the label is kept either way.
    """
    if not username:
        return None
    return apps.get_model(settings.AUTH_USER_MODEL).objects.filter(username=username).first()


def log_human(task, event_type, agent, message, human_actor=None):
    """Task event whose message carries the human label and whose FK names the user."""
    return task.log(
        event_type,
        agent,
        audit_message(message, human_actor),
        actor_user=human_user(human_actor),
    )


def validate_status_details(status, reason, result_summary):
    if status == Task.Status.BLOCKED and not reason.strip():
        raise TaskError("A blocked task requires a reason", 400)
    if status in {Task.Status.REVIEW, Task.Status.DONE} and not result_summary.strip():
        raise TaskError("Review and done require a result summary", 400)


@retry_sqlite
@transaction.atomic
def create_task(
    agent,
    project,
    title,
    description="",
    priority=100,
    depends_on=(),
    human_actor=None,
    card=None,
    handoff_mode=Task.HandoffMode.OFF,
):
    """Create a task; every task is shown on its project's planning board.

    ``card`` links an existing planning card (queue execution); otherwise a card
    is created so MCP/API/admin tasks are visible in the project view.
    """
    dependencies = list(Task.objects.filter(pk__in=depends_on))
    if len(dependencies) != len(set(depends_on)):
        raise TaskError("One or more dependencies do not exist", 400)
    task = Task.objects.create(
        project=project,
        title=title,
        description=description,
        priority=priority,
        handoff_mode=handoff_mode,
        created_by=agent,
    )
    task.depends_on.set(dependencies)
    attach_planning_card(task, card)
    log_human(task, TaskEvent.Type.CREATED, agent, "Task created", human_actor)
    return task


def set_claim(task, agent):
    now = timezone.now()
    # The conditional write is essential on SQLite, where SELECT FOR UPDATE is a no-op.
    changed = Task.objects.filter(
        pk=task.pk, status=Task.Status.PENDING, claimed_by__isnull=True
    ).update(
        status=Task.Status.IN_PROGRESS,
        claimed_by=agent,
        claimed_at=now,
        heartbeat_at=now,
        updated_at=now,
        blocked_reason="",
        result_summary="",
    )
    if not changed:
        raise TaskError("Task is no longer available")
    task.refresh_from_db()
    sync_planning_card(task)
    task.log(TaskEvent.Type.CLAIMED, agent)
    return task


@retry_sqlite
@transaction.atomic
def claim_next(agent, project=None):
    ready = Task.objects.ready()
    if project is not None:
        ready = ready.filter(project=project)
    task = locked(ready, skip_locked=True).first()
    return set_claim(task, agent) if task else None


@retry_sqlite
@transaction.atomic
def claim_task(agent, task_id):
    task = get_locked(task_id, skip_locked=True)
    if not task.is_ready:
        raise TaskError("Task is not ready or is already claimed")
    return set_claim(task, agent)


@retry_sqlite
@transaction.atomic
def heartbeat(agent, task_id, human_actor=None):
    task = get_locked(task_id)
    require_owner(task, agent, human_actor)
    if task.status != Task.Status.IN_PROGRESS:
        raise TaskError("Only in-progress tasks accept heartbeats")
    task.heartbeat_at = timezone.now()
    task.save(update_fields=["heartbeat_at", "updated_at"])
    log_human(task, TaskEvent.Type.HEARTBEAT, agent, "", human_actor)
    return task


@retry_sqlite
@transaction.atomic
def update_status(agent, task_id, status, reason="", result_summary="", human_actor=None):
    # Match dispatch's plan -> task lock order so cancellation cannot race a start.
    if status == Task.Status.CANCELLED:
        _lock_task_plans(task_id)
    task = get_locked(task_id)
    require_owner(task, agent, human_actor)
    if human_actor is None and task.status not in {
        Task.Status.IN_PROGRESS,
        Task.Status.BLOCKED,
        Task.Status.REVIEW,
    }:
        raise TaskError("Task is not active")
    if status not in {
        Task.Status.IN_PROGRESS,
        Task.Status.BLOCKED,
        Task.Status.REVIEW,
        Task.Status.DONE,
        Task.Status.CANCELLED,
    }:
        raise TaskError("Unsupported target status", 400)
    validate_status_details(status, reason, result_summary)
    if status == Task.Status.IN_PROGRESS and task.claimed_by_id is None:
        raise TaskError("In-progress tasks require a claiming agent", 400)
    previous = task.status
    task.status = status
    task.blocked_reason = reason if status == Task.Status.BLOCKED else ""
    task.result_summary = result_summary
    if status in {Task.Status.DONE, Task.Status.CANCELLED}:
        task.claimed_by = task.claimed_at = task.heartbeat_at = None
    elif status == Task.Status.IN_PROGRESS:
        task.heartbeat_at = timezone.now()
    task.save()
    if status == Task.Status.CANCELLED:
        _cancel_task_queue(task, agent, human_actor)
    sync_planning_card(task)
    log_human(
        task,
        TaskEvent.Type.STATUS_CHANGED,
        agent,
        f"{previous} -> {status}: {reason or result_summary}",
        human_actor,
    )
    return task


def clear_claim(task, agent, reason, actor_user=None):
    task.status = Task.Status.PENDING
    task.claimed_by = task.claimed_at = task.heartbeat_at = None
    task.blocked_reason = ""
    task.save()
    sync_planning_card(task)
    task.log(TaskEvent.Type.RELEASED, agent, reason, actor_user=actor_user)
    return task


@retry_sqlite
@transaction.atomic
def release_task(agent, task_id, reason="", human_actor=None):
    task = get_locked(task_id)
    require_owner(task, agent, human_actor)
    return clear_claim(
        task, agent, audit_message(reason, human_actor), actor_user=human_user(human_actor)
    )


@retry_sqlite
@transaction.atomic
def add_note(agent, task_id, message, human_actor=None):
    task = get_locked(task_id)
    if task.claimed_by_id is not None:
        require_owner(task, agent, human_actor)
    task.save(update_fields=["updated_at"])
    log_human(task, TaskEvent.Type.NOTE, agent, message, human_actor)
    return task


@retry_sqlite
@transaction.atomic
def release_stale_tasks(minutes=15):
    if minutes < 1:
        raise ValueError("minutes must be at least 1")
    count = 0
    for task in locked(Task.objects.stale(minutes), skip_locked=True):
        clear_claim(task, task.claimed_by, f"No heartbeat for {minutes} min")
        count += 1
    return count


def lock_admin_edits():
    """Serialize dependency graph edits for the transaction's lifetime."""
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT pg_advisory_xact_lock(721496302)")
        elif connection.vendor == "sqlite":
            # Acquire SQLite's writer reservation before reading the dependency graph.
            cursor.execute(
                "UPDATE coordinator_project SET name = name "
                "WHERE id = (SELECT MIN(id) FROM coordinator_project)"
            )


def validate_dependencies(task_id, dependencies):
    visited = set()
    stack = [task.pk for task in dependencies]
    while stack:
        pk = stack.pop()
        if pk == task_id:
            raise TaskError("Dependencies must not contain a cycle.", 400)
        if pk not in visited:
            visited.add(pk)
            stack.extend(Task.objects.get(pk=pk).depends_on.values_list("pk", flat=True))


@retry_sqlite
@transaction.atomic
def edit_task(agent, task_id, changes, human_actor=None):
    # Acquire the graph lock before any task row, consistently with the admin.
    lock_admin_edits()
    task = get_locked(task_id)
    require_owner(task, agent, human_actor)
    editable = {"title", "description", "project", "priority", "depends_on", "handoff_mode"}
    if set(changes) - editable:
        raise TaskError("Use lifecycle actions for protected task fields", 400)
    for name, value in changes.items():
        if name != "depends_on":
            setattr(task, name, value)
    if "depends_on" in changes:
        dependencies = changes["depends_on"]
        validate_dependencies(task.pk, dependencies)
        task.depends_on.set(dependencies)
    task.save()
    log_human(
        task, TaskEvent.Type.NOTE, agent, "Updated " + ", ".join(sorted(changes)), human_actor
    )
    return task


@transaction.atomic
def admin_save_task(instance, changed_fields, dependencies, username):
    """Apply only human-edited fields to current locked state, preserving live heartbeats."""
    creating = instance.pk is None
    if not creating:
        _lock_task_plans(instance.pk)
    task = instance if creating else get_locked(instance.pk)
    before = (
        {}
        if creating
        else {field.attname: getattr(task, field.attname) for field in Task._meta.concrete_fields}
    )
    if not creating:
        for field in Task._meta.concrete_fields:
            if field.name in changed_fields:
                setattr(task, field.attname, getattr(instance, field.attname))
    if task.status in {Task.Status.PENDING, Task.Status.DONE, Task.Status.CANCELLED}:
        task.claimed_by = None
    if task.claimed_by_id is None:
        task.claimed_at = task.heartbeat_at = None
    elif creating or before["claimed_by_id"] != task.claimed_by_id:
        task.claimed_at = task.heartbeat_at = timezone.now()
    elif task.status == Task.Status.IN_PROGRESS and before["status"] != task.status:
        task.heartbeat_at = timezone.now()
    if task.status != Task.Status.BLOCKED:
        task.blocked_reason = ""
    validate_status_details(task.status, task.blocked_reason, task.result_summary)
    task.full_clean(exclude=["depends_on"])
    task.save()
    if task.status == Task.Status.CANCELLED:
        _cancel_task_queue(task, None, username)
    if creating or "depends_on" in changed_fields:
        validate_dependencies(task.pk, dependencies)
        task.depends_on.set(dependencies)
    if creating:
        attach_planning_card(task)
    elif before["status"] != task.status or before["project_id"] != task.project_id:
        sync_planning_card(task)
    changes = []
    if not creating:
        for field in Task._meta.concrete_fields:
            if field.name != "updated_at" and before[field.attname] != getattr(task, field.attname):
                changes.append(field.name)
        if "depends_on" in changed_fields:
            changes.append("depends_on")
    event = TaskEvent.Type.CREATED if creating else TaskEvent.Type.NOTE
    if not creating and before["status"] != task.status:
        event = TaskEvent.Type.STATUS_CHANGED
    task.log(
        event,
        message=f"Human admin {username}: "
        + ("created task" if creating else "updated " + ", ".join(changes)),
        actor_user=human_user(username),
    )
    return task


# --- OpenCode worker registry (issues #7/#10) -------------------------------


def get_worker(name):
    worker = Worker.objects.filter(name=name).first()
    if worker is None:
        raise TaskError("Worker not found", 404)
    return worker


def verify_worker_write(worker_name, agent):
    """Bind an authenticated agent credential to its registered worker.

    Every worker write enforces this: the worker must exist, belong to the
    authenticating agent and remain enabled. A valid key cannot synchronize or
    upload command results for a different workerID (spoofing rejected with 403).
    """
    worker = get_worker(worker_name)
    if agent is None or worker.agent_id != agent.pk:
        raise TaskError("Credential is not bound to this worker", 403)
    if not worker.is_enabled:
        raise TaskError("Worker is disabled", 403)
    return worker


def clean_endpoint_url(value):
    """Normalize an endpoint URL and strip userinfo so credentials never persist."""
    if not isinstance(value, str):
        return ""
    text = value.strip()[:500]
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not (parts.hostname or ""):
        return ""
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/") or "", "", ""))


WORKER_META_TEXT_LIMITS = {
    "hostname": 255,
    "opencode_version": 64,
    "directory": 500,
    "create_directory": 500,
}


def normalize_codex_usage(value, now):
    """Keep the latest two account usage windows and their observation time."""
    if not isinstance(value, dict):
        return None
    sampled_at = value.get("sampled_at")
    windows = value.get("windows")
    if (
        type(sampled_at) is not int
        or not 0 < sampled_at <= int(now.timestamp()) + 60
        or not isinstance(windows, list)
        or not 1 <= len(windows) <= 2
    ):
        return None
    normalized = []
    for window in windows:
        if not isinstance(window, dict):
            return None
        remaining = window.get("remaining_percent")
        duration = window.get("duration_minutes")
        reset = window.get("resets_at")
        if type(remaining) not in (int, float) or not 0 <= remaining <= 100:
            return None
        if duration is not None and (type(duration) is not int or not 1 <= duration <= 525600):
            return None
        if reset is not None and (type(reset) is not int or not 0 < reset <= 253402300799):
            return None
        normalized.append(
            {
                "remaining_percent": remaining,
                "duration_minutes": duration,
                "resets_at": reset,
            }
        )
    return {"sampled_at": sampled_at, "windows": normalized}


@retry_sqlite
@transaction.atomic
def record_worker_meta(worker, meta):
    """Apply validated bridge metadata in one transaction; None keeps stored fields.

    `meta` is the validated meta record of the sync envelope (#11). This call is
    the worker heartbeat: only Worker.last_sync_at changes, never task claims.
    """
    now = timezone.now()
    version = meta.get("opencode_version")
    if version is None:
        version = meta.get("version")
    if version is not None:
        worker.opencode_version = str(version)[:64]
    for name, limit in WORKER_META_TEXT_LIMITS.items():
        value = meta.get(name)
        if name == "opencode_version":
            continue
        if value is not None:
            setattr(worker, name, str(value)[:limit])
    url = meta.get("opencode_url")
    if url is None:
        url = meta.get("endpoint")
    if url is not None:
        worker.opencode_url = clean_endpoint_url(url)
    version = meta.get("protocol_version")
    if isinstance(version, int) and version >= 0:
        worker.protocol_version = version
    if "connected" in meta:
        worker.connected = bool(meta.get("connected"))
    if "stream_connected" in meta:
        worker.stream_connected = bool(meta.get("stream_connected"))
    error = meta.get("error")
    if error is not None:
        worker.last_error = str(error)[:5000] if error else ""
        worker.last_error_at = now if error else None
    catalog = meta.get("models")
    if isinstance(catalog, list):
        worker.model_catalog = catalog
    agents = _sanitize_agent_catalog(meta.get("agents"))
    if agents is not None:
        worker.agent_catalog = agents
    default_model = meta.get("default_model")
    if isinstance(default_model, dict):
        worker.default_model = default_model
    for name in ("session_limit", "message_limit", "dropped_events"):
        value = meta.get(name)
        if isinstance(value, int) and 0 <= value <= 100000:
            setattr(worker, name, value)
    buffered = meta.get("buffered_events")
    # Bridge bounds: at most 1000 queued plus 1000 frozen in-flight events.
    if type(buffered) is int and 0 <= buffered <= 2000:
        worker.buffered_events = buffered
    # `usage` is the backend-neutral name (Codex and Claude workers); early Codex
    # bridges send only `codex_usage`. Both land in the same column.
    usage = normalize_codex_usage(meta.get("usage") or meta.get("codex_usage"), now)
    if usage and (not worker.codex_usage or usage["sampled_at"] > worker.codex_usage["sampled_at"]):
        worker.codex_usage = usage
    worker.last_sync_at = now
    worker.save()
    return worker


@retry_sqlite
@transaction.atomic
def provision_worker(name, agent, human_actor=None):
    """Explicitly create a worker bound to an existing active agent.

    No credential is generated here: the bridge uses the bound agent's existing
    Bearer key, whose raw value was already disclosed once by create_agent/admin.
    Provisioning and rotation stay explicit human actions.
    """
    if not name.strip() or len(name) > 100:
        raise TaskError("Worker name must contain 1–100 characters", 400)
    if agent is None or agent.pk is None or not agent.is_active:
        raise TaskError("Workers must be bound to an existing active agent", 400)
    if Worker.objects.filter(name=name).exists():
        raise TaskError("Worker name already exists", 409)
    if hasattr(agent, "worker"):
        raise TaskError("Agent is already bound to a worker", 409)
    return Worker.objects.create(name=name, agent=agent)


@retry_sqlite
@transaction.atomic
def set_worker_enabled(worker, enabled, reason=""):
    """Deactivation blocks further ingestion/delivery while preserving history."""
    worker.is_enabled = bool(enabled)
    worker.save(update_fields=["is_enabled", "updated_at"])
    return worker


def worker_read_model(worker, counts=False):
    """Credential-free worker read model; online derives from sync freshness."""
    now = timezone.now()
    data = {
        "id": worker.pk,
        "name": worker.name,
        "agent": str(worker.agent) if worker.agent_id else None,
        "is_enabled": worker.is_enabled,
        "online": worker.is_online(now),
        "hostname": worker.hostname,
        "opencode_url": worker.opencode_url,
        "protocol_version": worker.protocol_version,
        "opencode_version": worker.opencode_version,
        "directory": worker.directory,
        "create_directory": worker.create_directory,
        "connected": worker.connected,
        "stream_connected": worker.stream_connected,
        "last_error": worker.last_error,
        "last_error_at": worker.last_error_at,
        "default_model": worker.default_model,
        "models": worker.model_catalog,
        "agents": worker.agent_catalog,
        "session_limit": worker.session_limit,
        "message_limit": worker.message_limit,
        "dropped_events": worker.dropped_events,
        "buffered_events": worker.buffered_events,
        "last_sync_at": worker.last_sync_at,
        "created_at": worker.created_at,
    }
    if counts:
        data["counts"] = {
            "sessions": worker.sessions.count(),
            "commands": worker.commands.count(),
            "events": worker.events.count(),
            "pending": worker.pending_interactions.filter(resolved_at=None).count(),
        }
        # Pending interaction cards (issues #14): resolved requests disappear only
        # through a complete authoritative snapshot.
        data["pending_interactions"] = [
            interaction_card(interaction)
            for interaction in worker.pending_interactions.filter(resolved_at=None).order_by("-id")[
                :MAX_PENDING_READ
            ]
        ]
    return data


# --- Observed sessions, messages and events (issues #7/#9) ------------------


def normalize_session_record(record):
    """Accept either POC key spellings and produce normalized session fields."""
    if not isinstance(record, dict):
        raise TaskError("Invalid session record", 400)
    remote_id = record.get("sessionID") or record.get("id")
    if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 128:
        raise TaskError("Each session needs a remote id", 400)
    time_info = record.get("time") if isinstance(record.get("time"), dict) else {}
    model = record.get("model") if isinstance(record.get("model"), dict) else None
    return {
        "remote_id": remote_id,
        "directory": str(record.get("directory") or "")[:500],
        "title": str(record.get("title") or "")[:500],
        "agent_name": str(record.get("agentName") or record.get("agent") or "")[:100],
        "model": model,
        "remote_parent_id": str(record.get("parentID") or "")[:128],
        "execution_status": str(record.get("status") or "")[:32],
        "remote_created_ms": time_info.get("created")
        if isinstance(time_info.get("created"), int)
        else None,
        "remote_updated_ms": time_info.get("updated")
        if isinstance(time_info.get("updated"), int)
        else None,
    }


def upsert_session(worker, record, now=None):
    """Create or refresh one observed session with monotonic snapshot fields."""
    now = now or timezone.now()
    fields = normalize_session_record(record)
    session, created = WorkerSession.objects.get_or_create(
        worker=worker,
        remote_id=fields["remote_id"],
        defaults={"first_seen_at": now, "last_seen_at": now, **fields},
    )
    if not created:
        stale = (
            fields["remote_updated_ms"] is not None
            and session.remote_updated_ms is not None
            and fields["remote_updated_ms"] < session.remote_updated_ms
        )
        if not stale:  # never regress a newer observed snapshot
            for name, value in fields.items():
                setattr(session, name, value)
    session.last_seen_at = now
    resolve_parent_link(session)
    session.save()
    # Late parent observation: newly seen parents claim their unresolved subagents.
    if session.remote_id:
        WorkerSession.objects.filter(
            worker=worker, remote_parent_id=session.remote_id, parent_id=None
        ).exclude(pk=session.pk).update(parent=session)
    return session


@retry_sqlite
@transaction.atomic
def store_sessions(worker, records):
    now = timezone.now()
    sessions = []
    for record in records:
        session = upsert_session(worker, record, now)
        # Async correlation: attempts awaiting this remote id adopt the row (#13).
        TaskExecutionAttempt.objects.filter(
            worker=worker, remote_session_id=session.remote_id, session=None
        ).update(session=session)
        sessions.append(session)
    return sessions


# --- Project planning and durable sequential execution (#28) -----------------


def planning_column_for(columns, task):
    """Board column matching a task's lifecycle, or None when the board lacks it.

    Columns are operator-editable, so only the default names are mapped; cards on
    custom boards keep whatever column the operator chose.
    """
    if task.status == Task.Status.PENDING:
        candidates = ["Ready", "Backlog"] if task.is_ready else ["Backlog", "Ready"]
    elif task.status in {Task.Status.DONE, Task.Status.CANCELLED}:
        candidates = ["Completed"]
    else:
        candidates = ["In progress"]
    return next((c for c in candidates if c in columns), None)


def attach_planning_card(task, card=None):
    """Give a new task its project-board card (creating the plan if needed)."""
    if card is None:
        plan, _ = ProjectPlan.objects.get_or_create(project=task.project)
        column = planning_column_for(plan.columns, task) or plan.columns[0]
        card = PlanningCard(
            plan=plan,
            title=task.title,
            description=task.description,
            column=column,
            position=plan.cards.filter(column=column, archived=False).count(),
        )
    card.task = task
    card.save()
    return card


def sync_planning_card(task):
    """Move a task's board card to the column for its new status."""
    card = PlanningCard.objects.filter(task=task, archived=False).select_related("plan").first()
    if card is None:
        return
    if card.plan.project_id != task.project_id:
        # An admin moved the task to another project; its card follows it.
        if card.jobs.filter(status__in=["queued", "running"]).exists():
            raise TaskError("Remove queued planning work before moving the task", 409)
        card.plan, _ = ProjectPlan.objects.get_or_create(project=task.project)
        card.column = None
    column = planning_column_for(card.plan.columns, task)
    if card.column is None:
        column = column or card.plan.columns[0]
    if column and column != card.column:
        card.column = column
        card.position = card.plan.cards.filter(column=column, archived=False).count()
        card.save(update_fields=["plan", "column", "position", "updated_at"])


def _pipe_jobs(pipe):
    """Planning jobs executing a pipe's stages; Pipe has no direct jobs relation."""
    return PlanningJob.objects.filter(pipe_stage__pipe=pipe)


def _pipe_read(pipe):
    """Bounded dashboard view of a pipe, its stages, artifacts and latest jobs."""
    stages = []
    for stage in pipe.stages.select_related("worker", "card__task").order_by("position", "pk"):
        job = (
            stage.jobs.select_related("attempt__task", "worker")
            .order_by("-position", "-pk")
            .first()
        )
        attempt = job.attempt if job else None
        stages.append(
            {
                "id": stage.pk,
                "position": stage.position,
                "name": stage.name,
                "role": stage.role,
                "status": stage.status,
                "instructions": stage.instructions,
                "worker": stage.worker.name,
                "agent": stage.agent,
                "model": stage.model,
                "directory": stage.directory,
                "requires_approval": stage.requires_approval,
                "artifact": stage.artifact,
                "attempts": stage.attempts,
                "started_at": _iso(stage.started_at) if stage.started_at else None,
                "completed_at": _iso(stage.completed_at) if stage.completed_at else None,
                "card": stage.card_id,
                "task": attempt.task_id
                if attempt
                else (stage.card.task_id if stage.card_id else None),
                "job": job.pk if job else None,
                "job_status": job.status if job else None,
                "job_detail": job.detail[:500] if job else "",
                "session": attempt.remote_session_id if attempt else "",
                "task_status": attempt.task.status
                if attempt
                else (stage.card.task.status if stage.card_id and stage.card.task_id else None),
            }
        )
    return {
        "id": pipe.pk,
        "title": pipe.title,
        "description": pipe.description,
        "status": pipe.status,
        "detail": pipe.detail,
        "on_failure": pipe.on_failure,
        "max_retries": pipe.max_retries,
        "retries_used": pipe.retries_used,
        "approved_stage": pipe.approved_stage,
        "archived": pipe.archived,
        "stages": stages,
    }


def planning_read(project):
    from .models import planning_columns

    plan = ProjectPlan.objects.filter(project=project).first()
    settings_card = {
        "worker": plan.default_worker.name if plan and plan.default_worker_id else "",
        "directory": plan.directory if plan else "",
        "model": plan.model if plan else None,
        "agent": plan.agent if plan else "",
        "context": plan.context if plan else "",
        "instructions": plan.instructions if plan else "",
        "handoff_mode": plan.handoff_mode if plan else Task.HandoffMode.OFF,
        "templates": plan.templates if plan else [],
        "columns": plan.columns if plan else planning_columns(),
    }
    cards = []
    jobs = []
    if plan:
        for card in plan.cards.filter(archived=False).select_related("task", "worker"):
            cards.append(
                {
                    "id": card.pk,
                    "title": card.title,
                    "description": card.description,
                    "column": card.column,
                    "position": card.position,
                    "labels": card.labels,
                    "checklist": card.checklist,
                    "instructions": card.instructions,
                    "worker": card.worker.name if card.worker_id else "",
                    "agent": card.agent,
                    "model": card.model,
                    # The card's own override ("" inherits) and what it resolves
                    # to, so the board can badge a card without re-deriving it.
                    "handoff_mode": card.handoff_mode,
                    "effective_handoff_mode": _card_handoff(plan, card),
                    "task": card.task_id,
                    "task_status": card.task.status if card.task else None,
                }
            )
        history = list(
            plan.jobs.exclude(status__in=["queued", "running"])
            .order_by("-id")
            .values_list("pk", flat=True)[:50]
        )
        for job in plan.jobs.filter(
            Q(status__in=["queued", "running"]) | Q(pk__in=history)
        ).select_related("card", "worker", "attempt__task", "pipe_stage__pipe"):
            attempt = job.attempt
            jobs.append(
                {
                    "id": job.pk,
                    "card": job.card_id,
                    "title": job.snapshot.get("title", job.card.title),
                    "position": job.position,
                    "status": job.status,
                    "detail": job.detail,
                    "worker": job.worker.name,
                    "agent": job.snapshot.get("agent", ""),
                    "model": job.snapshot.get("model"),
                    "snapshot": job.snapshot,
                    "prompt": job.prompt,
                    "task": attempt.task_id if attempt else None,
                    "session": attempt.remote_session_id if attempt else "",
                    "task_status": attempt.task.status if attempt else None,
                    "pipe": job.pipe_stage.pipe_id if job.pipe_stage_id else None,
                    "stage": (
                        {
                            "id": job.pipe_stage_id,
                            "position": job.pipe_stage.position,
                            "name": job.pipe_stage.name,
                            "role": job.pipe_stage.role,
                        }
                        if job.pipe_stage_id
                        else None
                    ),
                }
            )
    pipes = []
    if plan:
        for pipe in (
            plan.pipes.filter(archived=False)
            .prefetch_related("stages__worker")
            .select_related("plan")[:20]
        ):
            pipes.append(_pipe_read(pipe))
    return {
        "project": project.slug,
        "name": project.name,
        "settings": settings_card,
        "revision": plan.revision if plan else 0,
        "playing": plan.playing if plan else False,
        "pause_reason": plan.pause_reason if plan else "",
        "cards": cards,
        "jobs": sorted(jobs, key=lambda row: (row["position"], row["id"])),
        "pipes": pipes,
    }


def pipes_read(project, limit=20, offset=0):
    """Bounded, newest-first pipe list for read-only MCP and API clients."""
    plan = ProjectPlan.objects.filter(project=project).first()
    if plan is None:
        return {
            "project": project.slug,
            "count": 0,
            "limit": limit,
            "offset": offset,
            "pipes": [],
        }
    queryset = plan.pipes.filter(archived=False).order_by("-pk")
    rows = queryset.prefetch_related("stages__worker").select_related("plan")[
        offset : offset + limit
    ]
    return {
        "project": project.slug,
        "count": queryset.count(),
        "limit": limit,
        "offset": offset,
        "pipes": [_pipe_read(pipe) for pipe in rows],
    }


def pipe_read(project, pipe_id):
    """One pipe from this project only; a foreign or missing id is a 404."""
    pipe = Pipe.objects.filter(plan__project=project, pk=pipe_id).select_related("plan").first()
    if pipe is None:
        raise TaskError("Pipe not found on this project", 404)
    return {"project": project.slug, "pipe": _pipe_read(pipe)}


@retry_sqlite
@transaction.atomic
def mutate_planning(project, actor, data):
    """Staff-only caller; serialize board/queue edits with optimistic revision checks."""
    plan, _ = ProjectPlan.objects.get_or_create(project=project)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    if data["revision"] != plan.revision:
        raise TaskError("Project changed. Refresh before saving your changes.", 409)
    operation = data["operation"]
    if operation == "pipe_create":
        raw = dict(data["pipe"])
        play = bool(raw.pop("play", False))
        _create_pipe(plan, actor, raw, play=play)
    elif operation == "pipe_approve":
        pipe = plan.pipes.filter(pk=data.get("pipe_id")).first()
        if not pipe:
            raise TaskError("Pipe not found on this project", 404)
        _approve_pipe(plan, pipe, actor)
    elif operation == "pipe_stop":
        pipe = plan.pipes.filter(pk=data.get("pipe_id")).first()
        if not pipe:
            raise TaskError("Pipe not found on this project", 404)
        if pipe.status in {Pipe.Status.COMPLETED, Pipe.Status.CANCELLED}:
            raise TaskError(f"Pipe '{pipe.title}' is already {pipe.status}", 409)
        _stop_pipe(plan, pipe, actor)
    elif operation == "pipe_edit":
        values = dict(data["pipe_edit"])
        pipe = plan.pipes.filter(pk=values.pop("pipe_id")).first()
        if not pipe:
            raise TaskError("Pipe not found on this project", 404)
        _edit_pipe(plan, pipe, actor, values)
    elif operation == "pipe_remove":
        pipe = plan.pipes.filter(pk=data.get("pipe_id")).first()
        if not pipe:
            raise TaskError("Pipe not found on this project", 404)
        _remove_pipe(plan, pipe, actor)
    elif operation == "settings":
        values = data["settings"]
        worker = Worker.objects.filter(name=values.pop("worker", "")).first()
        values["agent"] = (values.get("agent") or "").strip()
        if values["agent"] and (not worker or not _agent_allowed(worker, values["agent"])):
            raise TaskError("Agent is not available on the selected worker", 400)
        if values.get("model") and (not worker or not _model_allowed(worker, values["model"])):
            raise TaskError("Model is not available on the selected worker", 400)
        if any(c.column not in values["columns"] for c in plan.cards.filter(archived=False)):
            raise TaskError("Move cards before removing their column", 400)
        plan.default_worker = worker
        for field, value in values.items():
            setattr(plan, field, value)
    elif operation == "card":
        values = data["card"]
        card_id = values.pop("id", None)
        card = plan.cards.filter(pk=card_id, archived=False).first() if card_id else None
        if card_id and not card:
            raise TaskError("Card not found", 404)
        if values["column"] not in plan.columns:
            raise TaskError("Unknown board column", 400)
        if "worker" in values:
            values["worker"] = Worker.objects.filter(name=values["worker"]).first()
        if "agent" in values:
            values["agent"] = (values["agent"] or "").strip()
        effective_worker = (
            values.get("worker", card.worker if card else None) or plan.default_worker
        )
        effective_agent = values.get("agent", card.agent if card else "")
        effective_model = values.get("model", card.model if card else None)
        if effective_worker:
            if effective_agent and not _agent_allowed(effective_worker, effective_agent):
                raise TaskError("Agent is not available on the card's worker", 400)
            if effective_model and not _model_allowed(effective_worker, effective_model):
                raise TaskError("Model is not available on the card's worker", 400)
        if card is None:
            card = PlanningCard(plan=plan, position=plan.cards.count())
        for field, value in values.items():
            setattr(card, field, value)
        plan.cards.filter(column=card.column, position__gte=card.position).exclude(
            pk=card.pk
        ).update(position=F("position") + 1)
        card.save()
        _refresh_queued_job(plan, card)
        if card.task_id:
            log_human(card.task, TaskEvent.Type.NOTE, None, "Planning card updated", actor)
    elif operation in {"enqueue", "run"}:
        card = plan.cards.filter(pk=data.get("card_id"), archived=False).first()
        if not card:
            raise TaskError("Card not found", 404)
        if plan.jobs.filter(card=card, status__in=["queued", "running"]).exists():
            raise TaskError("This card already has queued or running work", 409)
        if plan.jobs.filter(status__in=["queued", "running"]).count() >= 100:
            raise TaskError("Project queue is full (100 jobs)", 429)
        if operation == "run" and plan.jobs.filter(status__in=["queued", "running"]).exists():
            raise TaskError("Use Play to run the existing queue first", 409)
        worker, agent, model = _card_runner(plan, card)
        if card.task_id and card.task.status in {Task.Status.DONE, Task.Status.CANCELLED}:
            raise TaskError(
                "This card's task is terminal. Create a new card for follow-up work.", 409
            )
        snapshot = {
            **_card_snapshot(card),
            "context": plan.context,
            "standing_instructions": plan.instructions,
            "instructions": data.get("instructions", card.instructions),
            "template": data.get("template", ""),
            "directory": plan.directory or worker.create_directory or worker.directory,
            "agent": agent,
            "model": model,
            "handoff_mode": _card_handoff(plan, card),
            "project": project.slug,
            "repo_url": project.repo_url,
            "docs_path": project.docs_path,
        }
        PlanningJob.objects.create(
            plan=plan,
            card=card,
            worker=worker,
            snapshot=snapshot,
            position=(
                plan.jobs.order_by("-position").values_list("position", flat=True).first() or 0
            )
            + 1,
            actor=actor,
            actor_user=human_user(actor),
        )
        if operation == "run":
            plan.playing = True
            plan.pause_reason = ""
    elif operation == "reorder":
        ids = data["job_ids"]
        queued = list(plan.jobs.filter(status="queued").values_list("pk", flat=True))
        if len(ids) != len(set(ids)) or set(ids) != set(queued):
            raise TaskError("Reorder must include every queued job exactly once", 409)
        stage_positions = {}
        for job in plan.jobs.filter(
            pk__in=ids, status="queued", pipe_stage__isnull=False
        ).select_related("pipe_stage"):
            stage_positions.setdefault(job.pipe_stage.pipe_id, []).append(
                (ids.index(job.pk), job.pipe_stage.position)
            )
        for sequence in stage_positions.values():
            positions = [position for _, position in sorted(sequence)]
            if positions != sorted(positions):
                raise TaskError("Keep pipe stages in their configured order", 409)
        base = (
            plan.jobs.exclude(status="queued")
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
            or 0
        ) + 1
        for offset, pk in enumerate(ids):
            plan.jobs.filter(pk=pk).update(position=base + offset)
    elif operation == "remove":
        job = plan.jobs.filter(pk=data.get("job_id")).select_related("card__task").first()
        if not job:
            raise TaskError("Queue job not found", 404)
        if job.status != "queued":
            raise TaskError("Only queued jobs can be removed", 409)
        if job.pipe_stage_id:
            raise TaskError("This job is a pipe stage; stop the pipe instead of removing it", 409)
        job.status = "cancelled"
        job.detail = f"Removed from queue by {actor}; task status unchanged"
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "detail", "completed_at"])
        if job.card.task_id:
            log_human(
                job.card.task,
                TaskEvent.Type.NOTE,
                None,
                f"Queue job #{job.pk}: {job.detail}",
                actor,
            )
    elif operation == "archive":
        card = plan.cards.filter(pk=data.get("card_id")).first()
        if not card or card.jobs.filter(status__in=["queued", "running"]).exists():
            raise TaskError("Remove queued work before archiving a card", 409)
        card.archived = True
        card.save(update_fields=["archived"])
    elif operation == "play":
        awaiting = plan.pipes.filter(status=Pipe.Status.AWAITING_APPROVAL, archived=False).first()
        if awaiting:
            raise TaskError(
                f"Pipe '{awaiting.title}' awaits checkpoint approval. "
                "Approve it instead of pressing Play.",
                409,
            )
        if not plan.jobs.filter(status__in=["queued", "running"]).exists():
            raise TaskError("Add tasks to the queue before pressing Play", 409)
        plan.playing = True
        plan.pause_reason = ""
    elif operation in {"pause", "stop"}:
        plan.playing = False
        plan.pause_reason = "Paused by operator"
        if operation == "stop":
            job = (
                plan.jobs.filter(status="running")
                .select_related("attempt", "worker", "pipe_stage__pipe")
                .first()
            )
            if job:
                if not job.attempt.remote_session_id:
                    raise TaskError(
                        "Session creation is still in flight. "
                        "Pause, then stop once the session is known.",
                        409,
                    )
                stop_command, _ = enqueue_worker_command(
                    job.worker.agent,
                    actor,
                    job.worker,
                    "interrupt",
                    {},
                    remote_session_id=job.attempt.remote_session_id,
                    client_request_id=f"planning-stop-{job.pk}",
                )
                job.status = "failed"
                job.stop_command = stop_command
                job.detail = (
                    "Stopped by operator; task remains owned until explicitly resolved or released."
                )
                job.save(update_fields=["status", "detail", "stop_command"])
                if job.pipe_stage_id:
                    _mark_pipe_stopped(
                        plan,
                        job.pipe_stage.pipe,
                        f"Pipe '{job.pipe_stage.pipe.title}' stopped by operator; "
                        "task work is not completed",
                        actor,
                    )
    else:
        raise TaskError("Unsupported planning operation", 400)
    plan.revision += 1
    plan.save()
    transaction.on_commit(
        lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
    )


def _card_runner(plan, card):
    """Worker, agent and model a card runs with (#314).

    The card's own worker overrides the project default. Project agent/model
    defaults belong to the project worker; another worker falls back to its own
    defaults unless the card names its own.
    """
    worker = card.worker or plan.default_worker
    if not worker:
        raise TaskError("Configure a project worker or choose one for this card", 400)
    if not worker.is_enabled or not worker.agent_id or not worker.agent.is_active:
        raise TaskError(f"Worker {worker.name} is not enabled", 400)
    inherit = worker.pk == plan.default_worker_id
    agent = (card.agent or (plan.agent if inherit else "") or "").strip()
    if agent and not _agent_allowed(worker, agent):
        raise TaskError(f"Agent is not available on worker {worker.name}", 400)
    model = card.model or (plan.model if inherit else None) or worker.default_model
    if model and not _model_allowed(worker, model):
        raise TaskError(f"Model is not available on worker {worker.name}", 400)
    return worker, agent, model


# --- PM router queue actions (#333) -------------------------------------------
# Voice requests reuse mutate_planning's enqueue/play paths; the real dispatch
# still happens only in tick_planning_queues during authenticated worker sync.
# Callers run these inside one transaction, so a refusal leaves no mutation.

TERMINAL_STATUSES = {Task.Status.DONE, Task.Status.CANCELLED}


def model_label(model):
    return f"{model['providerID']}/{model['modelID']}" if model else ""


def catalog_models(worker, limit=None):
    """Connectable models a worker reported, as provider/model labels."""
    labels = []
    for provider in worker.model_catalog or []:
        if not isinstance(provider, dict):
            continue
        nested = provider.get("models")
        if isinstance(nested, dict):
            for item in nested.values():
                if isinstance(item, dict) and item.get("id") and item.get("connectable", True):
                    labels.append(f"{provider.get('id')}/{item['id']}")
        elif provider.get("providerID") and provider.get("modelID"):
            labels.append(model_label(provider))
    return labels[:limit] if limit else labels


def worker_ready(worker):
    return bool(
        worker.is_enabled
        and worker.agent_id
        and worker.agent.is_active
        and worker.is_online()
        and worker.connected
        and worker.stream_connected
    )


def worker_brief(worker):
    return {
        "name": worker.name,
        "online": worker_ready(worker),
        "enabled": worker.is_enabled,
        "default_model": model_label(worker.default_model),
        "models": catalog_models(worker, 25),
        "model_count": len(catalog_models(worker)),
    }


def _resolve_worker(name):
    worker = Worker.objects.select_related("agent").filter(name=name).first()
    worker = worker or Worker.objects.select_related("agent").filter(name__iexact=name).first()
    if not worker:
        known = ", ".join(Worker.objects.order_by("name").values_list("name", flat=True)[:20])
        raise TaskError(f"Unknown worker '{name}'. Registered workers: {known or 'none'}", 400)
    return worker


def _parse_model(worker, value):
    """Strict model check: voice never forwards a model the worker has not reported."""
    provider, _, model_id = value.strip().partition("/")
    if not provider or not model_id:
        raise TaskError("Model must be written as provider/model, e.g. openai/gpt-5", 400)
    model = {"providerID": provider, "modelID": model_id}
    if not catalog_models(worker):
        raise TaskError(
            f"Worker {worker.name} has not reported a model catalog, so model {value} "
            "cannot be verified",
            400,
        )
    if not _model_allowed(worker, model):
        raise TaskError(f"Model {value} is not in worker {worker.name}'s model catalog", 400)
    return model


def _require_ready_worker(worker, what):
    if not worker_ready(worker):
        raise TaskError(
            f"Worker {worker.name} is offline or disconnected, so {what} cannot start now. "
            "Nothing was changed; do not retry until the worker reconnects.",
            409,
        )


def _require_no_blocking_failure(plan):
    """Same rule _advance_planning uses to re-pause; refuse instead of a fake start."""
    for failed in plan.jobs.filter(status="failed").select_related("attempt__task"):
        if failed.attempt_id and (
            failed.attempt.task.status not in TERMINAL_STATUSES
            or WorkerCommand.objects.filter(
                pk__in=[
                    failed.attempt.create_command_id,
                    failed.attempt.prompt_command_id,
                    failed.stop_command_id,
                ],
                status__in=["queued", "dispatched"],
            ).exists()
        ):
            raise TaskError(
                f"The queue is held by failed or stopped task #{failed.attempt.task_id} "
                f"({failed.attempt.task.status}); resolve it on the board first. "
                "Nothing was changed.",
                409,
            )


def _job_brief(job):
    attempt = job.attempt
    return {
        "job": job.pk,
        "title": job.snapshot.get("title", job.card.title),
        "status": job.status,
        "task": attempt.task_id if attempt else job.card.task_id,
        "task_status": attempt.task.status
        if attempt
        else (job.card.task.status if job.card.task_id else None),
        "session": attempt.remote_session_id if attempt else "",
        "worker": job.worker.name,
        "worker_online": worker_ready(job.worker),
        "model": model_label(job.snapshot.get("model")) or "worker default",
        "agent": job.snapshot.get("agent") or "worker default",
        "directory": job.snapshot.get("directory", ""),
        "detail": job.detail[:500],
        "pipe": job.pipe_stage.pipe_id if job.pipe_stage_id else None,
        "stage": (
            {
                "id": job.pipe_stage_id,
                "position": job.pipe_stage.position,
                "name": job.pipe_stage.name,
                "role": job.pipe_stage.role,
            }
            if job.pipe_stage_id
            else None
        ),
    }


def _pipe_brief(pipe):
    stages = []
    for stage in pipe.stages.select_related("worker").order_by("position", "pk"):
        job = stage.jobs.order_by("-position", "-pk").first()
        stages.append(
            {
                "position": stage.position,
                "name": stage.name,
                "role": stage.role,
                "status": stage.status,
                "instructions": stage.instructions,
                "worker": stage.worker.name,
                "model": model_label(stage.model) or "worker default",
                "requires_approval": stage.requires_approval,
                "artifact": stage.artifact[:1000],
                "job": job.pk if job else None,
                "job_status": job.status if job else None,
            }
        )
    return {
        "pipe": pipe.pk,
        "title": pipe.title,
        "status": pipe.status,
        "detail": pipe.detail[:500],
        "on_failure": pipe.on_failure,
        "retries_used": pipe.retries_used,
        "max_retries": pipe.max_retries,
        "awaiting_approval": pipe.status == Pipe.Status.AWAITING_APPROVAL,
        "stages": stages,
    }


def queue_summary(plan):
    active = list(
        plan.jobs.filter(status__in=["queued", "running"]).select_related(
            "card", "worker__agent", "attempt__task", "pipe_stage__pipe"
        )
    )
    recent = list(
        plan.jobs.exclude(status__in=["queued", "running"])
        .select_related("card", "worker__agent", "attempt__task", "pipe_stage__pipe")
        .order_by("-id")[:5]
    )
    if not active:
        state = "empty"
    elif plan.playing:
        state = "playing"
    else:
        state = "paused"
    return {
        "project": plan.project.slug,
        "state": state,
        "playing": plan.playing,
        "pause_reason": plan.pause_reason,
        "default_worker": plan.default_worker.name if plan.default_worker_id else "",
        "directory": plan.directory,
        "jobs": [_job_brief(job) for job in active[:20]],
        "active_count": len(active),
        "recent": [_job_brief(job) for job in recent],
        "pipes": [
            _pipe_brief(pipe)
            for pipe in plan.pipes.filter(archived=False)
            .exclude(status__in=[Pipe.Status.COMPLETED, Pipe.Status.CANCELLED])
            .prefetch_related("stages__worker")
            .order_by("-pk")[:10]
        ],
        "note": (
            "queued means accepted and waiting; running means a session was requested on the "
            "worker; only task status done means the work was reported complete"
        ),
    }


def voice_queue_read(project):
    plan, _ = ProjectPlan.objects.get_or_create(project=project)
    workers = Worker.objects.select_related("agent").filter(is_enabled=True).order_by("name")[:10]
    return {**queue_summary(plan), "workers": [worker_brief(w) for w in workers]}


@transaction.atomic
def voice_create_pipe(project, actor, data):
    """Queue a multi-stage pipe from the PM router; nothing runs before sync."""
    plan, _ = ProjectPlan.objects.get_or_create(project=project)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    play = bool(data.get("play", True))
    pipe = _create_pipe(plan, actor, data, play=play)
    plan.revision += 1
    plan.save(update_fields=["revision", "playing", "pause_reason"])
    transaction.on_commit(
        lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
    )
    first_job = _pipe_jobs(pipe).order_by("position").first()
    ahead = plan.jobs.filter(
        status__in=["queued", "running"], position__lt=first_job.position if first_job else 0
    ).count()
    return {
        "pipe": pipe.pk,
        "title": pipe.title,
        "project": project.slug,
        "status": pipe.status,
        "stages": [
            {
                "position": stage.position,
                "name": stage.name,
                "role": stage.role,
                "worker": stage.worker.name,
                "model": model_label(stage.model) or "worker default",
                "directory": stage.directory,
                "requires_approval": stage.requires_approval,
            }
            for stage in pipe.stages.select_related("worker").order_by("position")
        ],
        "on_failure": pipe.on_failure,
        "max_retries": pipe.max_retries,
        "first_job": first_job.pk if first_job else None,
        "jobs_ahead": ahead,
        "queue_playing": plan.playing,
        "state": (
            "Pipe accepted and queued; the first stage starts on its worker's next sync. "
            "Nothing has completed yet"
            if plan.playing
            else "Pipe added to the queue but it is not playing; nothing will start"
        ),
    }


@transaction.atomic
def voice_approve_pipe(project, actor, pipe_id):
    plan = ProjectPlan.objects.filter(project=project).first()
    if not plan:
        raise TaskError(f"Project {project.slug} has no pipe queue", 409)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    pipe = plan.pipes.filter(pk=pipe_id).first()
    if not pipe:
        raise TaskError(f"Unknown pipe #{pipe_id} in project {project.slug}", 404)
    _approve_pipe(plan, pipe, actor)
    plan.revision += 1
    plan.save(update_fields=["revision", "playing", "pause_reason"])
    transaction.on_commit(
        lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
    )
    pipe.refresh_from_db()
    plan.refresh_from_db()
    return {
        "pipe": pipe.pk,
        "title": pipe.title,
        "status": pipe.status,
        "approved_stage": pipe.approved_stage,
        "queue_playing": plan.playing,
        "state": "Checkpoint approved; the next stage is queued and starts on its worker's next "
        "sync. Nothing has completed yet.",
    }


@transaction.atomic
def voice_stop_pipe(project, actor, pipe_id):
    plan = ProjectPlan.objects.filter(project=project).first()
    if not plan:
        raise TaskError(f"Project {project.slug} has no pipe queue", 409)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    pipe = plan.pipes.filter(pk=pipe_id).first()
    if not pipe:
        raise TaskError(f"Unknown pipe #{pipe_id} in project {project.slug}", 404)
    if pipe.status in {Pipe.Status.COMPLETED, Pipe.Status.CANCELLED}:
        raise TaskError(f"Pipe '{pipe.title}' is already {pipe.status}", 409)
    job = _stop_pipe(plan, pipe, actor)
    plan.revision += 1
    plan.save(update_fields=["revision", "playing", "pause_reason"])
    transaction.on_commit(
        lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
    )
    stop_command = job.stop_command if job and job.stop_command_id else None
    return {
        "pipe": pipe.pk,
        "title": pipe.title,
        "status": pipe.status,
        "interrupt": (
            {"id": stop_command.pk, "status": stop_command.status} if stop_command else None
        ),
        "state": (
            f"Pipe paused; interrupt command #{stop_command.pk} accepted and queued. "
            "Interruption is not confirmed; task work is not completed."
            if stop_command
            else "Pipe stopped; no session was running, so no interrupt was queued. "
            "Waiting stages were cancelled; no task was completed."
        ),
    }


@transaction.atomic
def voice_launch_task(task, actor, worker="", model="", start=True):
    """Queue a task's planning card and optionally Play its project queue."""
    if task.status in TERMINAL_STATUSES:
        raise TaskError(f"Task #{task.pk} is {task.status}; create a new task instead", 409)
    if task.status != Task.Status.PENDING or task.claimed_by_id:
        raise TaskError(f"Task #{task.pk} is already {task.status} and owned; not relaunched", 409)
    if not task.is_ready:
        raise TaskError(f"Task #{task.pk} has unfinished dependencies, so it cannot start", 409)
    plan, _ = ProjectPlan.objects.get_or_create(project=task.project)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    card = PlanningCard.objects.filter(task=task, archived=False).first()
    if card is None:
        card = attach_planning_card(task)
    if card.plan_id != plan.pk:
        raise TaskError("Task's planning card belongs to another project", 409)
    if worker:
        card.worker = _resolve_worker(worker)
    if model:
        runner = card.worker or plan.default_worker
        if not runner:
            raise TaskError("Choose a worker: the project has no default worker", 400)
        card.model = _parse_model(runner, model)
    runner, agent, effective_model = _card_runner(plan, card)
    active = list(
        plan.jobs.filter(status__in=["queued", "running"]).select_related("worker__agent")
    )
    if start:
        _require_no_blocking_failure(plan)
        _require_ready_worker(runner, f"task #{task.pk}")
        if active:
            _require_ready_worker(active[0].worker, "the queue")
    if worker or model:
        card.save(update_fields=["worker", "model", "updated_at"])
    mutate_planning(
        plan.project,
        actor,
        {"revision": plan.revision, "operation": "enqueue", "card_id": card.pk},
    )
    plan.refresh_from_db()
    if start and not plan.playing:
        mutate_planning(plan.project, actor, {"revision": plan.revision, "operation": "play"})
        plan.refresh_from_db()
    job = plan.jobs.filter(card=card, status="queued").select_related("worker").get()
    ahead = [row for row in active if row.position < job.position]
    log_human(
        task,
        TaskEvent.Type.NOTE,
        None,
        f"Queued for execution by PM router on worker {runner.name}, model "
        f"{model_label(effective_model) or 'worker default'}"
        + ("; queue playing" if plan.playing else "; queue not started"),
        actor,
    )
    return {
        "task": task.pk,
        "title": task.title,
        "project": plan.project.slug,
        "job": job.pk,
        "job_status": job.status,
        "jobs_ahead": len(ahead),
        "worker": runner.name,
        "worker_online": worker_ready(runner),
        "model": model_label(effective_model) or "worker default",
        "agent": agent or "worker default",
        "directory": job.snapshot.get("directory", ""),
        "queue_playing": plan.playing,
        "state": (
            "accepted and queued; the worker starts it on its next sync. Not started or "
            "completed yet"
            if plan.playing
            else "added to the queue but the queue is not playing; nothing will start"
        ),
    }


@transaction.atomic
def voice_play_queue(project, actor):
    plan = ProjectPlan.objects.filter(project=project).first()
    if plan is None:
        raise TaskError(f"Project {project.slug} has no queue yet; queue a task first", 409)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    active = list(
        plan.jobs.filter(status__in=["queued", "running"]).select_related(
            "worker__agent", "attempt__task", "card__task"
        )
    )
    if not active:
        raise TaskError(f"The {project.slug} queue is empty; nothing to play", 409)
    head = active[0]
    _require_no_blocking_failure(plan)
    if head.status == "running" and (
        not head.attempt_id
        or head.attempt.task.status != Task.Status.IN_PROGRESS
        or head.attempt.task.claimed_by_id != head.attempt.owner_id
    ):
        raise TaskError(
            f"Queue job #{head.pk} is unresolved; resolve its task before resuming. "
            "Nothing was changed.",
            409,
        )
    _require_ready_worker(head.worker, "the queue")
    previous_reason = plan.pause_reason
    already = plan.playing
    if not already:
        mutate_planning(project, actor, {"revision": plan.revision, "operation": "play"})
        plan.refresh_from_db()
        task = head.attempt.task if head.attempt_id else head.card.task
        if task:
            log_human(task, TaskEvent.Type.NOTE, None, "Project queue played by PM router", actor)
    return {
        **queue_summary(plan),
        "already_playing": already,
        "cleared_pause_reason": "" if already else previous_reason,
        "state": (
            "queue playing; the next queued job starts on the worker's next sync. "
            "Nothing has completed yet"
        ),
    }


def _lock_task_plans(task_id):
    list(
        locked(
            ProjectPlan.objects.filter(
                pk__in=PlanningCard.objects.filter(task_id=task_id).values("plan_id")
            ).order_by("pk")
        )
    )


def _cancel_task_queue(task, agent=None, actor=None):
    """Caller holds plan then task locks. Running work requires explicit Stop."""
    for job in PlanningJob.objects.filter(card__task=task, status="queued"):
        job.status = "cancelled"
        job.detail = f"Task #{task.pk} cancelled; queued execution invalidated"
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "detail", "completed_at"])
        ProjectPlan.objects.filter(pk=job.plan_id).update(revision=F("revision") + 1)
        log_human(task, TaskEvent.Type.NOTE, agent, f"Queue job #{job.pk}: {job.detail}", actor)
        transaction.on_commit(
            lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
        )


@transaction.atomic
def voice_edit_queue(project, actor, operation, job_id=None, worker="", model="", agent=None):
    """Owner voice gate lives at the capability boundary; reuse board mutations."""
    plan = ProjectPlan.objects.filter(project=project).first()
    if not plan:
        raise TaskError(f"The {project.slug} queue is empty", 409)
    plan = locked(ProjectPlan.objects.filter(pk=plan.pk)).get()
    job = None
    if operation in {"remove", "retarget"}:
        job = (
            plan.jobs.select_related("card__task", "worker__agent", "attempt__task")
            .filter(pk=job_id)
            .first()
        )
        if not job:
            raise TaskError(f"Unknown queue job #{job_id} in project {project.slug}", 404)
        if job.status != "queued":
            raise TaskError(
                f"Job #{job.pk} is {job.status}; {operation} is only allowed for queued jobs", 409
            )
        if operation == "retarget":
            card = job.card
            if card.task_id and card.task.status in TERMINAL_STATUSES:
                raise TaskError(
                    f"Task #{card.task_id} is {card.task.status}; remove its stale job instead", 409
                )
            if worker:
                card.worker = _resolve_worker(worker)
            runner = card.worker or plan.default_worker
            if not runner:
                raise TaskError("Choose a worker; no project default is configured", 400)
            _require_ready_worker(runner, "retargeting the queued job")
            if model:
                card.model = _parse_model(runner, model)
            if agent is not None:
                if agent and (not runner.agent_catalog or not _agent_allowed(runner, agent)):
                    raise TaskError(
                        f"Agent {agent} is not in worker {runner.name}'s agent catalog", 400
                    )
                card.agent = agent
            # Validate the complete effective runner before persisting any field.
            _card_runner(plan, card)
            card.save(update_fields=["worker", "model", "agent", "updated_at"])
            _refresh_queued_job(plan, card)
            plan.revision += 1
            plan.save(update_fields=["revision"])
            transaction.on_commit(
                lambda: dispatch_notification("planning.changed", subject="planning"), robust=True
            )
            job.refresh_from_db()
            job.detail = f"Retargeted by PM router ({actor}); still queued, not started"
            job.save(update_fields=["detail"])
        else:
            mutate_planning(
                project, actor, {"revision": plan.revision, "operation": "remove", "job_id": job.pk}
            )
            job.refresh_from_db()
    else:
        active = list(
            plan.jobs.filter(status__in=["queued", "running"]).select_related(
                "card__task", "worker__agent", "attempt__task"
            )
        )
        if not active:
            raise TaskError(f"The {project.slug} queue is empty; nothing to {operation}", 409)
        job = next((j for j in active if j.status == "running"), active[0])
        if operation == "stop" and job.status == "running":
            _require_ready_worker(job.worker, "stopping the current session")
        mutate_planning(project, actor, {"revision": plan.revision, "operation": operation})
        job.refresh_from_db()
    plan.refresh_from_db()
    affected = _job_brief(job)
    task = job.attempt.task if job.attempt_id else job.card.task
    message = (
        f"PM router {operation}: job #{job.pk}, task #{affected['task']}, "
        f"worker {affected['worker']}, model {affected['model']}, "
        f"agent {affected['agent']}, directory {affected['directory']}"
    )
    if task:
        log_human(task, TaskEvent.Type.NOTE, None, message, actor)
    interrupt = job.stop_command if operation == "stop" and job.stop_command_id else None
    return {
        **queue_summary(plan),
        "action": operation,
        "affected": affected,
        "interrupt": {"id": interrupt.pk, "status": interrupt.status} if interrupt else None,
        "state": {
            "remove": (
                "Removed from the execution queue; task status unchanged. "
                "Not executed or completed."
            ),
            "retarget": "Retargeted and still queued; not started or completed.",
            "pause": "Queue paused; current work may continue, no next job will launch.",
            "stop": (
                f"Queue paused; interrupt command #{interrupt.pk} accepted and queued. "
                "Interruption is not confirmed; task is not completed or released."
                if interrupt
                else "Queue paused; no session is running, so no interrupt was queued. "
                "Waiting jobs remain queued; no task was completed or released."
            ),
        }[operation],
    }


def _card_handoff(plan, card):
    """Hand-off mode a card launches with (#137): its override, else the project default.

    Deliberately not worker-scoped, unlike agent/model in ``_card_runner``: who
    answers an approve/deny prompt is not a worker capability, so a card running
    on a non-default worker still inherits the project's choice.
    """
    return card.handoff_mode or plan.handoff_mode


def _card_snapshot(card):
    return {
        "title": card.title,
        "description": card.description,
        "checklist": card.checklist,
        "labels": card.labels,
    }


def _refresh_queued_job(plan, card):
    """Card edits reach its not-yet-started job (#321).

    Project context, instructions and directory stay frozen at enqueue; the
    card's own content and worker/agent/model choices apply until it starts.
    A running job already has its session and is left alone.
    """
    job = plan.jobs.filter(card=card, status="queued").first()
    if job is None:
        return
    worker, agent, model = _card_runner(plan, card)
    snapshot = {
        **job.snapshot,
        **_card_snapshot(card),
        "agent": agent,
        "model": model,
        "handoff_mode": _card_handoff(plan, card),
    }
    if worker.pk != job.worker_id and not plan.directory:
        # The frozen directory came from the old worker's defaults.
        snapshot["directory"] = worker.create_directory or worker.directory
    job.worker = worker
    job.snapshot = snapshot
    job.save(update_fields=["worker", "snapshot"])


def _pipe_stage_config(plan, raw, position):
    """Validate one stage without mutating anything (#437).

    Refusals happen before the first card or job is written, so a bad stage
    cannot leave a half-created pipe behind.
    """
    name = (raw.get("name") or f"Stage {position}").strip()[:100]
    role = raw.get("role") or PipeStage.Role.CUSTOM
    if role not in PipeStage.Role.values:
        raise TaskError(f"Unknown pipe stage role '{role}'", 400)
    worker_name = (raw.get("worker") or "").strip()
    if worker_name:
        worker = _resolve_worker(worker_name)
    else:
        worker = plan.default_worker
    if not worker:
        raise TaskError(f"Stage '{name}' needs a worker; the project has no default worker", 400)
    agent = (raw.get("agent") or "").strip()
    if agent and not _agent_allowed(worker, agent):
        raise TaskError(f"Agent {agent} is not available on worker {worker.name}", 400)
    model = raw.get("model")
    if model and not _model_allowed(worker, model):
        raise TaskError(
            f"Model {model_label(model)} is not in worker {worker.name}'s model catalog", 400
        )
    _require_ready_worker(worker, f"pipe stage '{name}'")
    directory = (
        (raw.get("directory") or "").strip()
        or plan.directory
        or worker.create_directory
        or worker.directory
    )
    if not directory:
        raise TaskError(f"Stage '{name}' has no directory; configure one for the worker", 400)
    card = None
    card_id = raw.get("card_id")
    if card_id:
        card = plan.cards.filter(pk=card_id, archived=False).first()
        if not card:
            raise TaskError(f"Stage '{name}' card not found on this project", 404)
        if plan.jobs.filter(card=card, status__in=["queued", "running"]).exists():
            raise TaskError(f"Stage '{name}' card already has queued or running work", 409)
        if card.task_id and card.task.status in {Task.Status.DONE, Task.Status.CANCELLED}:
            raise TaskError(
                f"Stage '{name}' card's task is terminal; create a new card for this stage", 409
            )
    return {
        "name": name,
        "role": role,
        "instructions": (raw.get("instructions") or "").strip(),
        "worker": worker,
        "agent": agent,
        "model": model,
        "directory": directory,
        "requires_approval": bool(raw.get("requires_approval")),
        "card": card,
    }


def _pipe_stage_card(plan, pipe, stage):
    """Card (and so eventual task) a stage runs on; created once per attempt."""
    column = plan.columns[0]
    return PlanningCard.objects.create(
        plan=plan,
        title=f"{pipe.title} · {stage.name}"[:300],
        description=stage.instructions or pipe.description,
        column=column,
        position=plan.cards.filter(column=column, archived=False).count(),
        instructions=stage.instructions,
        worker=stage.worker,
        agent=stage.agent,
        model=stage.model,
        handoff_mode="",
    )


def _admit_pipe_stage(plan, pipe, stage, actor=None, after_position=None):
    """Queue one stage as an ordinary PlanningJob.

    When ``after_position`` is given the next stage is inserted directly after
    the job that just finished, shifting later queued jobs by one so the pipe's
    stages stay contiguous in the durable order.
    """
    if stage.status in {PipeStage.Status.QUEUED, PipeStage.Status.RUNNING}:
        return stage.jobs.filter(status__in=["queued", "running"]).order_by("-id").first()
    card = stage.card or _pipe_stage_card(plan, pipe, stage)
    stage.card = card
    if after_position is None:
        position = (
            plan.jobs.order_by("-position").values_list("position", flat=True).first() or 0
        ) + 1
    else:
        position = after_position + 1
        plan.jobs.filter(status="queued", position__gte=position).update(position=F("position") + 1)
    snapshot = {
        **_card_snapshot(card),
        "context": plan.context,
        "standing_instructions": plan.instructions,
        "instructions": stage.instructions or card.instructions,
        "template": "",
        "directory": stage.directory,
        "agent": stage.agent,
        "model": stage.model,
        "handoff_mode": _card_handoff(plan, card),
        "project": plan.project.slug,
        "repo_url": plan.project.repo_url,
        "docs_path": plan.project.docs_path,
        "pipe": {"id": pipe.pk, "title": pipe.title},
        "stage": {
            "id": stage.pk,
            "position": stage.position,
            "name": stage.name,
            "role": stage.role,
            "total": pipe.stages.count(),
        },
    }
    job = PlanningJob.objects.create(
        plan=plan,
        card=card,
        worker=stage.worker,
        snapshot=snapshot,
        pipe_stage=stage,
        position=position,
        actor=actor or pipe.actor,
        actor_user=pipe.actor_user,
    )
    stage.status = PipeStage.Status.QUEUED
    stage.attempts += 1
    stage.save(update_fields=["card", "status", "attempts"])
    if pipe.status in {Pipe.Status.QUEUED, Pipe.Status.RUNNING}:
        pipe.status = Pipe.Status.RUNNING
        pipe.save(update_fields=["status", "updated_at"])
    return job


def _validate_pipe_policy(on_failure, max_retries, roles):
    if on_failure not in Pipe.Failure.values:
        raise TaskError("Pipe failure policy must be stop or loop", 400)
    if not 0 <= max_retries <= 5:
        raise TaskError("Pipe max_retries must be between 0 and 5", 400)
    if on_failure == Pipe.Failure.LOOP:
        test_index = next((i for i, role in enumerate(roles) if role == PipeStage.Role.TEST), None)
        if test_index is None:
            raise TaskError("A looping pipe needs a test stage", 400)
        if PipeStage.Role.EXECUTE not in roles[:test_index]:
            raise TaskError("A looping pipe needs an execute stage before its test stage", 400)
        if max_retries < 1:
            raise TaskError("A looping pipe needs at least one retry", 400)


def _create_pipe(plan, actor, data, play=False):
    """Create a pipe, its stage cards and its first queued stage job (#437)."""
    title = (data.get("title") or "").strip()
    if not title:
        raise TaskError("A pipe needs a title", 400)
    raw_stages = data.get("stages") or []
    if not 1 <= len(raw_stages) <= 10:
        raise TaskError("A pipe needs between 1 and 10 stages", 400)
    on_failure = data.get("on_failure") or Pipe.Failure.STOP
    max_retries = data.get("max_retries", 1)
    configs = [
        _pipe_stage_config(plan, raw, index) for index, raw in enumerate(raw_stages, start=1)
    ]
    _validate_pipe_policy(on_failure, max_retries, [config["role"] for config in configs])
    names = [config["name"] for config in configs]
    if len(set(names)) != len(names):
        raise TaskError("Pipe stage names must be unique", 400)
    card_ids = [config["card"].pk for config in configs if config["card"]]
    if len(set(card_ids)) != len(card_ids):
        raise TaskError("Each pipe stage needs a different card", 400)
    pipe = Pipe.objects.create(
        plan=plan,
        title=title[:300],
        description=(data.get("description") or "").strip(),
        on_failure=on_failure,
        max_retries=max_retries,
        actor=actor,
        actor_user=human_user(actor),
    )
    stages = []
    for index, config in enumerate(configs, start=1):
        stage = PipeStage.objects.create(
            pipe=pipe,
            position=index,
            name=config["name"],
            role=config["role"],
            instructions=config["instructions"],
            worker=config["worker"],
            agent=config["agent"],
            model=config["model"],
            directory=config["directory"],
            requires_approval=config["requires_approval"],
        )
        if config["card"]:
            stage.card = config["card"]
            stage.save(update_fields=["card"])
        else:
            stage.card = _pipe_stage_card(plan, pipe, stage)
            stage.save(update_fields=["card"])
        stages.append(stage)
    _admit_pipe_stage(plan, pipe, stages[0], actor=actor)
    if play:
        plan.playing = True
        plan.pause_reason = ""
    return pipe


def _clone_pipe_stage_card(plan, pipe, stage):
    column = stage.card.column if stage.card else plan.columns[0]
    return PlanningCard.objects.create(
        plan=plan,
        title=f"{pipe.title} · {stage.name} · retry {pipe.retries_used}"[:300],
        description=stage.instructions or pipe.description,
        column=column,
        position=plan.cards.filter(column=column, archived=False).count(),
        instructions=stage.instructions,
        worker=stage.worker,
        agent=stage.agent,
        model=stage.model,
    )


def _pipe_stage_log(stage, actor, message):
    task_id = stage.card.task_id if stage.card_id else None
    if not task_id:
        return
    task = Task.objects.filter(pk=task_id).first()
    if task:
        log_human(task, TaskEvent.Type.NOTE, None, message, actor)


def _advance_pipe(plan, job, task):
    """Record a completed stage's artifact and admit the next stage.

    Only an explicit task done reaches here; the queue has already marked the
    job completed. A checkpoint pauses the plan and Play is refused until an
    explicit approve operation.
    """
    stage = job.pipe_stage
    pipe = stage.pipe
    stage.status = PipeStage.Status.COMPLETED
    stage.artifact = (task.result_summary or job.detail or "")[:10000]
    stage.completed_at = timezone.now()
    stage.save(update_fields=["status", "artifact", "completed_at"])
    next_stage = pipe.stages.filter(position__gt=stage.position).order_by("position").first()
    _pipe_stage_log(
        stage,
        pipe.actor,
        f"Pipe '{pipe.title}' stage {stage.position}/{pipe.stages.count()} "
        f"'{stage.name}' completed; artifact handed to the next stage"
        if next_stage
        else f"Pipe '{pipe.title}' stage {stage.position} '{stage.name}' completed; pipe finished",
    )
    if next_stage is None:
        pipe.status = Pipe.Status.COMPLETED
        pipe.detail = f"All {pipe.stages.count()} stages completed"
        pipe.save(update_fields=["status", "detail", "updated_at"])
        return
    if stage.requires_approval and pipe.approved_stage < stage.position:
        pipe.status = Pipe.Status.AWAITING_APPROVAL
        pipe.detail = (
            f"Awaiting approval after stage {stage.position} '{stage.name}'. "
            "Approving resumes the pipe; Play alone will not."
        )
        pipe.save(update_fields=["status", "detail", "updated_at"])
        _pause_planning(plan, f"Pipe '{pipe.title}': {pipe.detail}")
        return
    _admit_pipe_stage(plan, pipe, next_stage, actor=pipe.actor, after_position=job.position)


def _handle_pipe_failure(plan, job, reason):
    """Apply the pipe's failure policy. Returns True when the pipe handled it."""
    stage = job.pipe_stage
    if not stage:
        return False
    pipe = stage.pipe
    reason = (reason or "Stage failed without a reason")[:10000]
    stage.status = PipeStage.Status.FAILED
    stage.artifact = reason
    stage.completed_at = timezone.now()
    stage.save(update_fields=["status", "artifact", "completed_at"])
    pipe.last_failure = reason
    looping = (
        pipe.on_failure == Pipe.Failure.LOOP
        and stage.role == PipeStage.Role.TEST
        and pipe.retries_used < pipe.max_retries
    )
    execute = (
        pipe.stages.filter(position__lt=stage.position, role=PipeStage.Role.EXECUTE)
        .order_by("-position")
        .first()
        if looping
        else None
    )
    if execute:
        pipe.retries_used += 1
        pipe.status = Pipe.Status.QUEUED
        pipe.detail = (
            f"Test stage failed; looping back to '{execute.name}' "
            f"(retry {pipe.retries_used}/{pipe.max_retries})"
        )
        pipe.save(update_fields=["status", "detail", "last_failure", "retries_used", "updated_at"])
        for retried in pipe.stages.filter(position__gte=execute.position).order_by("position"):
            retried.card = _clone_pipe_stage_card(plan, pipe, retried)
            retried.status = PipeStage.Status.PENDING
            retried.artifact = ""
            retried.started_at = None
            retried.completed_at = None
            retried.save(update_fields=["card", "status", "artifact", "started_at", "completed_at"])
        job.status = "cancelled"
        job.completed_at = timezone.now()
        job.detail = (
            f"Pipe retry {pipe.retries_used}/{pipe.max_retries} after test failure: {reason[:500]}"
        )
        job.save(update_fields=["status", "detail", "completed_at"])
        execute.refresh_from_db()
        _pipe_stage_log(
            stage,
            pipe.actor,
            f"Pipe '{pipe.title}' test failed; retry {pipe.retries_used}/{pipe.max_retries} "
            f"queued from stage '{execute.name}'",
        )
        _admit_pipe_stage(plan, pipe, execute, actor=pipe.actor, after_position=job.position)
        return True
    pipe.status = Pipe.Status.FAILED
    pipe.detail = f"Failed at stage {stage.position} '{stage.name}': {reason[:1500]}"
    pipe.save(update_fields=["status", "detail", "last_failure", "updated_at"])
    job.status = "failed"
    job.completed_at = timezone.now()
    job.detail = reason[:2000]
    job.save(update_fields=["status", "detail", "completed_at"])
    _pause_planning(plan, f"Pipe '{pipe.title}' failed: {pipe.detail}")
    _pipe_stage_log(stage, pipe.actor, f"Pipe '{pipe.title}' failed: {pipe.detail}")
    return True


def _approve_pipe(plan, pipe, actor):
    if pipe.status != Pipe.Status.AWAITING_APPROVAL:
        raise TaskError(f"Pipe '{pipe.title}' is {pipe.status}; no checkpoint is waiting", 409)
    checkpoint = (
        pipe.stages.filter(status=PipeStage.Status.COMPLETED, requires_approval=True)
        .order_by("-position")
        .first()
    )
    if not checkpoint or checkpoint.position <= pipe.approved_stage:
        raise TaskError("No checkpoint is awaiting approval", 409)
    next_stage = pipe.stages.filter(position__gt=checkpoint.position).order_by("position").first()
    pipe.approved_stage = checkpoint.position
    if next_stage:
        last_job = checkpoint.jobs.order_by("-position").first()
        _admit_pipe_stage(
            plan,
            pipe,
            next_stage,
            actor=actor,
            after_position=last_job.position if last_job else None,
        )
        pipe.status = Pipe.Status.RUNNING
        pipe.detail = f"Checkpoint after stage {checkpoint.position} approved"
        plan.playing = True
        plan.pause_reason = ""
    else:
        pipe.status = Pipe.Status.COMPLETED
        pipe.detail = "Checkpoint approved; no further stages"
    pipe.save(update_fields=["status", "detail", "approved_stage", "updated_at"])
    _pipe_stage_log(
        checkpoint,
        actor,
        f"Pipe '{pipe.title}' checkpoint after stage {checkpoint.position} "
        f"'{checkpoint.name}' approved by {actor}",
    )
    return pipe


def _mark_pipe_stopped(plan, pipe, detail, actor):
    pipe.status = Pipe.Status.FAILED
    pipe.detail = detail[:2000]
    pipe.save(update_fields=["status", "detail", "updated_at"])
    now = timezone.now()
    for stage in pipe.stages.filter(
        status__in=[PipeStage.Status.PENDING, PipeStage.Status.QUEUED, PipeStage.Status.RUNNING]
    ):
        stage.status = (
            PipeStage.Status.FAILED
            if stage.status == PipeStage.Status.RUNNING
            else PipeStage.Status.SKIPPED
        )
        stage.completed_at = now
        stage.save(update_fields=["status", "completed_at"])
    _pipe_jobs(pipe).filter(status="queued").update(
        status="cancelled", detail=f"Pipe stopped by {actor}", completed_at=now
    )
    plan.playing = False
    plan.pause_reason = detail[:2000]


def _stop_pipe(plan, pipe, actor):
    job = (
        _pipe_jobs(pipe)
        .filter(status="running")
        .select_related("attempt", "worker")
        .order_by("-position")
        .first()
    )
    if job:
        if not job.attempt_id or not job.attempt.remote_session_id:
            raise TaskError(
                "Session creation is still in flight. Pause, then stop once the session is known.",
                409,
            )
        stop_command, _ = enqueue_worker_command(
            job.worker.agent,
            actor,
            job.worker,
            "interrupt",
            {},
            remote_session_id=job.attempt.remote_session_id,
            client_request_id=f"planning-stop-{job.pk}",
        )
        job.status = "failed"
        job.stop_command = stop_command
        job.detail = (
            "Pipe stopped by operator; task remains owned until explicitly resolved or released."
        )
        job.save(update_fields=["status", "detail", "stop_command"])
    _mark_pipe_stopped(
        plan, pipe, f"Pipe '{pipe.title}' stopped by operator; task work is not completed", actor
    )
    return job


PIPE_EDITABLE_STATUSES = {
    Pipe.Status.QUEUED,
    Pipe.Status.RUNNING,
    Pipe.Status.AWAITING_APPROVAL,
}
PIPE_IMMUTABLE_STAGE_STATUSES = {PipeStage.Status.COMPLETED, PipeStage.Status.RUNNING}
PIPE_STAGE_FIELDS = (
    "name",
    "role",
    "instructions",
    "worker",
    "agent",
    "model",
    "directory",
    "requires_approval",
)


def _pipe_stage_values(stage):
    return {
        "name": stage.name,
        "role": stage.role,
        "instructions": stage.instructions,
        "worker": stage.worker.name,
        "agent": stage.agent,
        "model": stage.model,
        "directory": stage.directory,
        "requires_approval": stage.requires_approval,
    }


def _refresh_pipe_stage_job(plan, pipe, stage):
    """A queued stage job adopts its edited config; a running job is never touched."""
    job = stage.jobs.filter(status="queued").order_by("-position", "-pk").first()
    if job is None:
        return
    card = stage.card
    if card is not None:
        card.worker = stage.worker
        card.agent = stage.agent
        card.model = stage.model
        card.instructions = stage.instructions
        card.save(update_fields=["worker", "agent", "model", "instructions", "updated_at"])
    snapshot = {
        **job.snapshot,
        "agent": stage.agent,
        "model": stage.model,
        "directory": stage.directory,
        "instructions": stage.instructions or (card.instructions if card else ""),
        "stage": {
            "id": stage.pk,
            "position": stage.position,
            "name": stage.name,
            "role": stage.role,
            "total": pipe.stages.count(),
        },
    }
    job.worker = stage.worker
    job.snapshot = snapshot
    job.save(update_fields=["worker", "snapshot"])


def _ensure_pipe_progress(plan, pipe, actor, after_position=None):
    """Keep an edited active pipe moving after its only waiting stage was replaced."""
    if pipe.status not in {Pipe.Status.QUEUED, Pipe.Status.RUNNING}:
        return
    if _pipe_jobs(pipe).filter(status__in=["queued", "running"]).exists():
        return
    next_stage = (
        pipe.stages.exclude(status=PipeStage.Status.COMPLETED).order_by("position", "pk").first()
    )
    if next_stage is None:
        pipe.status = Pipe.Status.COMPLETED
        pipe.detail = f"All {pipe.stages.count()} stages completed"
        pipe.save(update_fields=["status", "detail", "updated_at"])
        return
    last_done = _pipe_jobs(pipe).filter(status="completed").order_by("-position").first()
    _admit_pipe_stage(
        plan,
        pipe,
        next_stage,
        actor=actor,
        after_position=after_position
        if after_position is not None
        else (last_done.position if last_done else None),
    )


def _edit_pipe(plan, pipe, actor, data):
    """Edit an active pipe without touching completed or running stages (#462)."""
    if pipe.archived:
        raise TaskError(f"Pipe '{pipe.title}' was removed; it cannot be edited", 409)
    if pipe.status not in PIPE_EDITABLE_STATUSES:
        raise TaskError(
            f"Pipe '{pipe.title}' is {pipe.status}; only queued, running or "
            "awaiting-approval pipes can be edited",
            409,
        )
    title = pipe.title
    if "title" in data:
        title = (data["title"] or "").strip()
        if not title:
            raise TaskError("A pipe needs a title", 400)
    description = data.get("description", pipe.description)
    on_failure = data.get("on_failure", pipe.on_failure)
    max_retries = data.get("max_retries", pipe.max_retries)
    if not 0 <= max_retries <= 5:
        raise TaskError("Pipe max_retries must be between 0 and 5", 400)
    existing = list(pipe.stages.select_related("worker").order_by("position", "pk"))
    by_id = {stage.pk: stage for stage in existing}
    updates = []
    desired = []
    seen = set()
    removed_names = []
    if "stages" in data:
        entries = data["stages"]
        if not 1 <= len(entries) <= 10:
            raise TaskError("A pipe needs between 1 and 10 stages", 400)
        for index, entry in enumerate(entries, start=1):
            stage_id = entry.get("id")
            if stage_id is not None:
                stage = by_id.get(stage_id)
                if stage is None:
                    raise TaskError(f"Stage #{stage_id} does not belong to this pipe", 404)
                if stage_id in seen:
                    raise TaskError("Each stage may appear only once", 409)
                seen.add(stage_id)
                values = _pipe_stage_values(stage)
                for key in PIPE_STAGE_FIELDS:
                    if key in entry:
                        values[key] = entry[key]
                if "worker" in entry:
                    values["worker"] = (entry["worker"] or "").strip()
                if stage.status in PIPE_IMMUTABLE_STAGE_STATUSES:
                    if values != _pipe_stage_values(stage):
                        raise TaskError(
                            f"Stage {stage.position} '{stage.name}' is {stage.status} "
                            "and cannot be edited, reordered or removed",
                            409,
                        )
                    desired.append(("stage", stage))
                    updates.append((stage, values, False))
                else:
                    config = _pipe_stage_config(plan, values, index)
                    desired.append(("stage", stage))
                    updates.append((stage, config, True))
            else:
                config = _pipe_stage_config(plan, entry, index)
                desired.append(("new", config))
        role_source = []
        for kind, item in desired:
            if kind == "stage":
                role_source.append(
                    next(values["role"] for stage, values, _ in updates if stage.pk == item.pk)
                )
            else:
                role_source.append(item["role"])
        names = [
            next(values["name"] for stage, values, _ in updates if stage.pk == item.pk)
            if kind == "stage"
            else item["name"]
            for kind, item in desired
        ]
        if len(set(names)) != len(names):
            raise TaskError("Pipe stage names must be unique", 400)
        _validate_pipe_policy(on_failure, max_retries, role_source)
        desired_ids = [item.pk for kind, item in desired if kind == "stage"]
        immutable_ids = [
            stage.pk for stage in existing if stage.status in PIPE_IMMUTABLE_STAGE_STATUSES
        ]
        if desired_ids[: len(immutable_ids)] != immutable_ids:
            raise TaskError(
                "Completed and running stages must stay first and in their current order",
                409,
            )
        for stage in existing:
            if stage.status == PipeStage.Status.QUEUED and stage.pk in seen:
                if desired_ids.index(stage.pk) + 1 != stage.position:
                    raise TaskError(
                        f"Stage {stage.position} '{stage.name}' is already queued to run next "
                        "and cannot be reordered; edit it or remove it instead",
                        409,
                    )
        replacement_after = None
        for stage in existing:
            if stage.status in PIPE_IMMUTABLE_STAGE_STATUSES or stage.pk in seen:
                continue
            removed_names.append(stage.name)
        # Mutate only after every refusal check above passed.
        pipe.stages.update(position=F("position") + 1000)
        for stage, values, editable in updates:
            if not editable:
                continue
            stage.name = values["name"]
            stage.role = values["role"]
            stage.instructions = values["instructions"]
            stage.worker = values["worker"]
            stage.agent = values["agent"]
            stage.model = values["model"]
            stage.directory = values["directory"]
            stage.requires_approval = values["requires_approval"]
            stage.save(
                update_fields=[
                    "name",
                    "role",
                    "instructions",
                    "worker",
                    "agent",
                    "model",
                    "directory",
                    "requires_approval",
                ]
            )
            if stage.card_id:
                PlanningCard.objects.filter(pk=stage.card_id).update(
                    worker=stage.worker,
                    agent=stage.agent,
                    model=stage.model,
                    instructions=stage.instructions,
                )
        for stage in existing:
            if stage.status in PIPE_IMMUTABLE_STAGE_STATUSES or stage.pk in seen:
                continue
            now = timezone.now()
            for job in stage.jobs.filter(status="queued"):
                job.status = "cancelled"
                job.detail = f"Stage removed from pipe '{pipe.title}' by {actor}"
                job.completed_at = now
                job.save(update_fields=["status", "detail", "completed_at"])
                if replacement_after is None or job.position - 1 < replacement_after:
                    replacement_after = job.position - 1
            if stage.card_id:
                PlanningCard.objects.filter(pk=stage.card_id, task__isnull=True).update(
                    archived=True
                )
            stage.delete()
        for offset, (kind, item) in enumerate(desired, start=1):
            if kind == "new":
                stage = PipeStage.objects.create(
                    pipe=pipe,
                    position=5000 + offset,
                    name=item["name"],
                    role=item["role"],
                    instructions=item["instructions"],
                    worker=item["worker"],
                    agent=item["agent"],
                    model=item["model"],
                    directory=item["directory"],
                    requires_approval=item["requires_approval"],
                )
                stage.card = _pipe_stage_card(plan, pipe, stage)
                stage.save(update_fields=["card"])
                item["stage"] = stage
        for offset, (kind, item) in enumerate(desired, start=1):
            stage = item if kind == "stage" else item["stage"]
            stage.position = offset
            stage.save(update_fields=["position"])
        for kind, item in desired:
            stage = item if kind == "stage" else item["stage"]
            if stage.status == PipeStage.Status.QUEUED:
                _refresh_pipe_stage_job(plan, pipe, stage)
    else:
        _validate_pipe_policy(
            on_failure,
            max_retries,
            [stage.role for stage in existing],
        )
    pipe.title = title[:300]
    pipe.description = (description or "").strip()
    pipe.on_failure = on_failure
    pipe.max_retries = max_retries
    detail = f"Edited by {actor}"
    if removed_names:
        detail += f"; removed stage(s): {', '.join(removed_names)}"
    pipe.detail = detail[:2000]
    pipe.save(
        update_fields=["title", "description", "on_failure", "max_retries", "detail", "updated_at"]
    )
    if "stages" in data:
        _ensure_pipe_progress(plan, pipe, actor, after_position=replacement_after)
    for kind, item in desired:
        stage = item if kind == "stage" else item["stage"]
        if stage.card_id and stage.card.task_id:
            log_human(
                stage.card.task,
                TaskEvent.Type.NOTE,
                None,
                f"Pipe '{pipe.title}' stage {stage.position} '{stage.name}' edited by {actor}",
                actor,
            )
    return pipe


def _remove_pipe(plan, pipe, actor):
    """Cancel waiting work and archive a pipe; a running stage must be stopped first."""
    if pipe.archived:
        raise TaskError(f"Pipe '{pipe.title}' was already removed", 409)
    if _pipe_jobs(pipe).filter(status="running").exists():
        raise TaskError(
            f"Pipe '{pipe.title}' has a running stage. Stop it first, then remove the pipe.",
            409,
        )
    now = timezone.now()
    stages = list(pipe.stages.order_by("position", "pk"))
    for stage in stages:
        if stage.status in {PipeStage.Status.PENDING, PipeStage.Status.QUEUED}:
            stage.status = PipeStage.Status.SKIPPED
            stage.completed_at = now
            stage.save(update_fields=["status", "completed_at"])
        if stage.card_id and stage.status == PipeStage.Status.SKIPPED:
            PlanningCard.objects.filter(pk=stage.card_id, task__isnull=True).update(archived=True)
    _pipe_jobs(pipe).filter(status="queued").update(
        status="cancelled",
        detail=f"Pipe removed by {actor}; waiting work cancelled",
        completed_at=now,
    )
    if pipe.status in PIPE_EDITABLE_STATUSES:
        pipe.status = Pipe.Status.CANCELLED
    pipe.archived = True
    pipe.detail = f"Removed by {actor}; waiting stages cancelled, history kept"
    pipe.save(update_fields=["status", "archived", "detail", "updated_at"])
    for stage in stages:
        if stage.card_id and stage.card.task_id:
            log_human(
                stage.card.task,
                TaskEvent.Type.NOTE,
                None,
                f"Pipe '{pipe.title}' removed by {actor}; stage {stage.position} was "
                f"{stage.status}",
                actor,
            )
    return pipe


def _pipe_prompt_section(job):
    stage = job.pipe_stage
    pipe = stage.pipe
    total = pipe.stages.count()
    earlier = []
    for earlier_stage in pipe.stages.filter(position__lt=stage.position).order_by("position"):
        if not earlier_stage.artifact:
            continue
        earlier.append(
            f"### Stage {earlier_stage.position}/{total} {earlier_stage.name} "
            f"({earlier_stage.role}, worker {earlier_stage.worker.name})\n"
            f"{earlier_stage.artifact[:8000]}"
        )
    retry = ""
    if pipe.last_failure and stage.position > 1:
        retry = (
            f"\n## Last failed test report\n{pipe.last_failure[:4000]}\n"
            "Use it to fix the implementation before re-running the test stage.\n"
        )
    policy = (
        f"loop back to execute with bounded retries ({pipe.retries_used}/{pipe.max_retries} used)"
        if pipe.on_failure == Pipe.Failure.LOOP
        else "stop and ask a human"
    )
    return (
        f"\n## Pipe: {pipe.title} — stage {stage.position}/{total} '{stage.name}' "
        f"({stage.role})\n{pipe.description or ''}\n"
        f"On failure: {policy}.\n"
        f"\n### Upstream artifacts\n"
        f"{chr(10).join(earlier) if earlier else 'None (this is the first stage).'}\n{retry}"
        "\n## Stage handoff\n"
        "Your final report summary is the artifact the next stage receives; it must state the "
        "concrete spec, files, behavior and verification produced. The next stage cannot see your "
        "session, so do not omit information it needs.\n"
    )


def _planning_prompt(job, task, report_token):
    previous = list(
        job.plan.jobs.filter(status="completed", position__lt=job.position)
        .select_related("attempt__task")
        .order_by("-position")[:10]
    )
    handoffs = "\n\n".join(
        f"{j.snapshot.get('title')}: {j.attempt.task.result_summary[:2000]}"
        for j in reversed(previous)
        if j.attempt_id
    )
    pipe_section = _pipe_prompt_section(job) if job.pipe_stage_id else ""
    return _planning_prompt_text(job, task, report_token, handoffs, pipe_section=pipe_section)


def _planning_prompt_text(job, task, report_token, handoffs, pipe_section=""):
    s = job.snapshot
    prompt = (
        f"# Project: {s['project']}\nRepository: {s['repo_url']}\nDocumentation: {s['docs_path']}\n"
        f"\n## Project context\n{s['context']}\n"
        f"\n## Standing instructions\n{s['standing_instructions']}\n"
        f"\n## Task #{task.pk}: {s['title']}\n{s['description']}\n"
        f"\nLabels: {', '.join(s['labels']) or 'None'}\n"
        f"\nChecklist / acceptance criteria:\n{json.dumps(s['checklist'], ensure_ascii=False)}\n"
        f"\n## Selected prompt\n{s['template']}\n\n## Your instructions\n{s['instructions']}\n"
        f"{pipe_section}"
        f"\n## Previous completed work\n{handoffs or 'None'}\n"
        f"\n## Coordination protocol\nTask {task.pk} is already claimed "
        f"by agent {job.worker.agent.name}. "
        "Do not claim a different task. Use the task-coordinator report_planning_job tool "
        f"with job_id={job.pk}, token={report_token}. This token is scoped to this job only. "
        "Call it with status=heartbeat every 2–5 minutes. "
        "When work and verification succeed, explicitly call it with status=done "
        "and a concrete summary/handoff. If blocked, use status=blocked with a summary; "
        "if review is required, use status=review with a summary. "
        "Never report done merely because the prompt was accepted. "
        "Do not put the job token in your result summary. If this tool is unavailable, "
        "ask the operator to complete the task through the planning queue."
    )
    return prompt


def _pause_planning(plan, reason):
    plan.playing = False
    plan.pause_reason = str(reason)[:2000]


def _advance_planning(plan, worker):
    # Repair historical/out-of-band cancellations even while paused. Never dispatch them.
    for task in Task.objects.filter(
        planningcard__jobs__plan=plan,
        planningcard__jobs__status="queued",
        status=Task.Status.CANCELLED,
    ).distinct():
        _cancel_task_queue(task)
    job = (
        plan.jobs.filter(status="running")
        .select_related("attempt__task", "card", "worker", "pipe_stage__pipe")
        .first()
    )
    if job:
        if job.worker_id != worker.pk:
            return
        attempt = job.attempt
        task = attempt.task
        commands = [attempt.create_command, attempt.prompt_command]
        unsettled = any(c and c.status in {"queued", "dispatched"} for c in commands)
        failure = next(
            (c for c in commands if c and (c.status == "error" or c.delivery_uncertain)), None
        )
        if failure or attempt.outcome in {"error", "uncertain"}:
            detail = (
                (failure.error or failure.uncertain_text) if failure else attempt.outcome_detail
            )
            if not _handle_pipe_failure(plan, job, detail):
                job.status = "failed"
                job.detail = detail
                _pause_planning(plan, job.detail or "Command failed; inspect before retrying")
        elif task.status == Task.Status.DONE:
            if unsettled or not attempt.prompt_command_id:
                _pause_planning(
                    plan,
                    "Task completed before command delivery was confirmed. "
                    "Reconcile commands before proceeding.",
                )
                return
            # Explicit task completion is the only successful queue-advancement signal.
            job.status = "completed"
            job.detail = task.result_summary
            job.completed_at = timezone.now()
            if job.pipe_stage_id:
                _advance_pipe(plan, job, task)
        elif task.status != Task.Status.IN_PROGRESS or task.claimed_by_id != attempt.owner_id:
            reason = task.blocked_reason or f"Task is {task.status}; resolve it before resuming"
            if not _handle_pipe_failure(plan, job, reason):
                _pause_planning(plan, reason)
        elif attempt.session and attempt.session.last_error:
            _pause_planning(plan, attempt.session.last_error)
        elif any(
            c
            and c.status == "dispatched"
            and c.dispatched_at
            and (timezone.now() - c.dispatched_at).total_seconds() > 120
            for c in commands
        ):
            _pause_planning(
                plan,
                "Command acknowledgement is overdue. "
                "Inspect Activity and reconcile; do not blindly replay.",
            )
        elif (
            attempt.remote_session_id
            and PendingInteraction.objects.filter(
                worker=worker, remote_session_id=attempt.remote_session_id, resolved_at__isnull=True
            ).exists()
        ):
            _pause_planning(
                plan, "Task needs your input. Open its session, answer, then press Play."
            )
        elif not attempt.prompt_command_id and attempt.remote_session_id:
            session = WorkerSession.objects.filter(
                worker=worker, remote_id=attempt.remote_session_id
            ).first()
            if session:
                link_attempt_session(attempt, session)
                payload = {"text": job.prompt}
                if job.snapshot.get("agent"):
                    payload["agent"] = job.snapshot["agent"]
                if job.snapshot.get("model"):
                    payload["model"] = job.snapshot["model"]
                enqueue_worker_command(
                    worker.agent,
                    job.actor,
                    worker,
                    "prompt",
                    payload,
                    remote_session_id=session.remote_id,
                    client_request_id=f"planning-prompt-{job.pk}",
                )
        job.save(update_fields=["status", "detail", "completed_at"])
        if job.status == "running":
            return
    if not plan.playing:
        return
    for failed in plan.jobs.filter(status="failed").select_related("attempt__task"):
        if failed.attempt_id and (
            failed.attempt.task.status not in {Task.Status.DONE, Task.Status.CANCELLED}
            or WorkerCommand.objects.filter(
                pk__in=[
                    failed.attempt.create_command_id,
                    failed.attempt.prompt_command_id,
                    failed.stop_command_id,
                ],
                status__in=["queued", "dispatched"],
            ).exists()
        ):
            _pause_planning(
                plan,
                "Resolve the failed/stopped task and any uncertain commands before continuing.",
            )
            return
    next_job = plan.jobs.filter(status="queued").select_related("card", "worker").first()
    if not next_job:
        plan.playing = False
        plan.pause_reason = "Queue completed"
        return
    if next_job.worker_id != worker.pk:
        return
    if not worker.is_online() or not worker.connected or not worker.stream_connected:
        return
    # Savepoint: rejected starts cannot leave a claimed task or orphan create command.
    try:
        with transaction.atomic():
            card = next_job.card
            # The card's configuration governs what a launch does (#137), so a
            # reused task adopts the queued hand-off mode instead of keeping a
            # stale one. Always present on jobs queued since migration 0024.
            handoff_mode = next_job.snapshot.get("handoff_mode") or Task.HandoffMode.OFF
            if card.task_id:
                task = get_locked(card.task_id, skip_locked=True)
                if not task.is_ready:
                    raise TaskError(
                        "Task is already owned or not ready; resolve it before launching"
                    )
                if task.handoff_mode != handoff_mode:
                    previous = task.handoff_mode
                    task.handoff_mode = handoff_mode
                    task.save(update_fields=["handoff_mode", "updated_at"])
                    log_human(
                        task,
                        TaskEvent.Type.NOTE,
                        worker.agent,
                        f"Hand-off mode {previous} -> {handoff_mode} from planning card",
                        next_job.actor,
                    )
            else:
                task = create_task(
                    worker.agent,
                    plan.project,
                    next_job.snapshot["title"],
                    next_job.snapshot["description"],
                    human_actor=next_job.actor,
                    card=card,
                    handoff_mode=handoff_mode,
                )
            claim_task(worker.agent, task.pk)
            report_token = secrets.token_urlsafe(32)
            prompt = _planning_prompt(next_job, task, report_token)
            attempt, command, _ = launch_task_execution(
                worker.agent,
                next_job.actor,
                task.pk,
                worker,
                directory=next_job.snapshot["directory"],
                client_request_id=f"planning-create-{next_job.pk}",
                title=next_job.snapshot["title"],
                model=next_job.snapshot.get("model"),
            )
            next_job.attempt = attempt
            next_job.prompt = prompt
            next_job.report_token_hash = hashlib.sha256(report_token.encode()).hexdigest()
            next_job.status = "running"
            next_job.save(update_fields=["attempt", "prompt", "report_token_hash", "status"])
            if next_job.pipe_stage_id:
                PipeStage.objects.filter(pk=next_job.pipe_stage_id).update(
                    status=PipeStage.Status.RUNNING
                )
    except TaskError as exc:
        _pause_planning(plan, exc.message)


def tick_planning_queues(worker):
    """Called inside authenticated worker sync, before dispatch. No browser scheduler.

    SKIP LOCKED avoids waiting on a plan held by another worker; one plan lock
    serializes every state transition and durable command idempotency key.
    Worker connectivity does not renew task claims.
    """
    plans = ProjectPlan.objects.filter(
        jobs__worker=worker, jobs__status__in=["queued", "running"]
    ).distinct()
    for pk in plans.values_list("pk", flat=True):
        plan = locked(ProjectPlan.objects.filter(pk=pk), skip_locked=True).first()
        if not plan:
            continue
        before = (
            plan.playing,
            plan.pause_reason,
            tuple(plan.jobs.filter(status__in=["queued", "running"]).values_list("pk", "status")),
        )
        try:
            with transaction.atomic():
                _advance_planning(plan, worker)
        except TaskError as exc:
            _pause_planning(plan, exc.message)
        # Running/queued rows are polled independently; revision changes only when
        # control state changes, so normal worker heartbeats don't invalidate edits.
        if before != (
            plan.playing,
            plan.pause_reason,
            tuple(plan.jobs.filter(status__in=["queued", "running"]).values_list("pk", "status")),
        ):
            plan.revision += 1
            plan.save(update_fields=["playing", "pause_reason", "revision"])


@retry_sqlite
@transaction.atomic
def report_planning_job(reporter, job_id, token, status, summary=""):
    # Lock order is plan -> task, matching queue orchestration. A job capability
    # delegates only reporting, never task ownership or other coordinator access.
    job = PlanningJob.objects.filter(pk=job_id).first()
    if (
        not job
        or not job.report_token_hash
        or not secrets.compare_digest(
            job.report_token_hash, hashlib.sha256(token.encode()).hexdigest()
        )
    ):
        raise TaskError("Unknown job or invalid report token", 404)
    locked(ProjectPlan.objects.filter(pk=job.plan_id)).get()
    job.refresh_from_db()
    if job.status != "running" or not job.attempt_id:
        raise TaskError("Job is not running", 409)
    task = get_locked(job.attempt.task_id)
    if task.claimed_by_id != job.attempt.owner_id:
        raise TaskError("Job no longer owns this task", 409)
    if status == "heartbeat":
        heartbeat(job.attempt.owner, task.pk)
    else:
        update_status(
            job.attempt.owner,
            task.pk,
            status,
            reason=summary if status == "blocked" else "",
            result_summary=summary,
        )
        task.log(
            TaskEvent.Type.NOTE,
            reporter,
            f"Planning job {job.pk} report by {reporter.name}: {status}",
        )
    return {"ok": True, "task": task.pk, "status": status}


def resolve_parent_link(session):
    """Link a subagent to its same-worker parent when that session is observed."""
    changed = False
    if session.remote_parent_id and session.parent_id is None:
        parent = WorkerSession.objects.filter(
            worker=session.worker, remote_id=session.remote_parent_id
        ).first()
        if parent and parent.pk != session.pk:
            session.parent = parent
            changed = True
    elif not session.remote_parent_id and session.parent_id is not None:
        session.parent = None
        changed = True
    return changed


def normalize_message_record(record):
    if not isinstance(record, dict):
        raise TaskError("Invalid message record", 400)
    info = record.get("info")
    info = info if isinstance(info, dict) else record
    remote_id = info.get("id") if isinstance(info.get("id"), str) else record.get("id")
    if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 128:
        raise TaskError("Each message needs a remote id", 400)
    time_info = info.get("time") if isinstance(info.get("time"), dict) else {}
    parts = record.get("parts")
    parts = parts if isinstance(parts, list) else []
    created = time_info.get("created")
    return {
        "remote_id": remote_id,
        "role": str(info.get("role") or "")[:16],
        "content": info
        if info is not record
        else {key: value for key, value in record.items() if key != "parts"},
        "parts": parts,
        "remote_time": created if isinstance(created, int) else None,
    }


@retry_sqlite
@transaction.atomic
def store_messages(worker, remote_session_id, message_records):
    """Upsert one session's published window; snapshots never regress rows.

    Upserts by (session, remote_message_id): known IDs replace content/parts
    (streaming), new IDs are inserted, and IDs absent from this window keep their
    stored rows with in_last_window=False (bounded recent window, not deletions).
    Window completeness is recorded per session for later cursor reads.
    """
    session = WorkerSession.objects.filter(worker=worker, remote_id=remote_session_id).first()
    if session is None:
        raise TaskError("Unknown session for this worker", 404)
    now = timezone.now()
    # A flush writes the latest state of each message once, not one SQL update
    # per streamed part. Duplicate IDs within a batch retain the final snapshot.
    normalized = {
        fields["remote_id"]: fields
        for fields in (normalize_message_record(record) for record in message_records)
    }
    window_ids = list(normalized)
    existing = {
        row.remote_id: row
        for row in WorkerMessage.objects.filter(session=session, remote_id__in=window_ids)
    }
    created, updated = [], []
    for remote_id, fields in normalized.items():
        row = existing.get(remote_id)
        if row is None:
            created.append(WorkerMessage(session=session, **fields, in_last_window=True))
        else:
            changed = (
                row.content != fields["content"]
                or row.parts != fields["parts"]
                or row.role != fields["role"]
                or row.remote_time != fields["remote_time"]
            )
            row.content = fields["content"]
            row.parts = fields["parts"]
            row.role = fields["role"]
            row.remote_time = fields["remote_time"]
            if changed:
                row.updated_at = now
            if changed or not row.in_last_window:
                row.in_last_window = True
                updated.append(row)
    WorkerMessage.objects.bulk_create(created, batch_size=100)
    WorkerMessage.objects.bulk_update(
        updated,
        ["content", "parts", "role", "remote_time", "in_last_window", "updated_at"],
        batch_size=100,
    )
    if window_ids:
        WorkerMessage.objects.filter(session=session).exclude(remote_id__in=window_ids).update(
            in_last_window=False
        )
    # Completeness semantics: a window shorter than the configured limit means the
    # bridge published everything it has for this session; a full window implies
    # older remote history exists that the server does not store.
    limit = worker.message_limit
    session.window_count = len(window_ids)
    session.window_oldest_id = window_ids[-1] if window_ids else ""
    session.window_complete = limit > 0 and len(window_ids) < limit
    session.last_seen_at = now
    session.save(
        update_fields=[
            "window_count",
            "window_oldest_id",
            "window_complete",
            "last_seen_at",
            "updated_at",
        ]
    )
    if created or updated:
        transaction.on_commit(
            lambda: dispatch_notification(
                "session.messages.flushed", subject="messages", worker=worker, session=session
            ),
            robust=True,
        )
    return session


def record_execution_error(session, message, at=None):
    """Persist the session's last execution error; cleared when execution restarts."""
    session.last_error = str(message)[:5000]
    session.last_error_at = at or timezone.now()
    session.save(update_fields=["last_error", "last_error_at", "updated_at"])
    return session


def clear_execution_error(session):
    """Documented clearing rule: a new execution start (busy) clears the error."""
    session.last_error = ""
    session.last_error_at = None
    session.save(update_fields=["last_error", "last_error_at", "updated_at"])


def event_body_hash(worker_id, remote_session_id, event_type, payload):
    digest = hashlib.sha256()
    digest.update(f"{worker_id}|{remote_session_id}|{event_type}|".encode())
    try:
        digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        digest.update(repr(payload).encode())
    return digest.hexdigest()


@retry_sqlite
@transaction.atomic
def store_events(worker, event_records):
    """Insert validated worker events with per-worker ring retention.

    Identical recent frames (same hash within the retained window) are skipped so
    unacknowledged sync retries or bridge restart republishing cannot duplicate
    history. Payloads exceeding WORKER_EVENT_MAX_BYTES are stored truncated with
    an explicit marker; this is a bounded archive, never a full replay promise.
    """
    limit = getattr(settings, "WORKER_EVENT_MAX_BYTES", 65536)
    session_ids = {record.get("sessionID") for record in event_records}
    known = {}
    if session_ids:
        for session in WorkerSession.objects.filter(worker=worker, remote_id__in=session_ids):
            known[session.remote_id] = session
    for record in event_records:
        if not isinstance(record, dict):
            raise TaskError("Invalid event record", 400)
    hashes = [
        event_body_hash(
            worker.pk,
            record.get("sessionID") or "",
            record.get("type") or "",
            record.get("payload") if record.get("payload") is not None else record.get("data"),
        )
        for record in event_records
    ]
    existing = set(
        WorkerEvent.objects.filter(worker=worker, body_hash__in=set(hashes)).values_list(
            "body_hash", flat=True
        )
    )
    created = []
    for record, body_hash in zip(event_records, hashes, strict=True):
        if body_hash in existing:
            continue
        existing.add(body_hash)
        remote_session_id = record.get("sessionID") or ""
        payload = record.get("payload")
        if payload is None:
            payload = record.get("data")
        size = len(json.dumps(payload, default=str)) if payload is not None else 0
        truncated = {}
        if size > limit:
            truncated = {
                "truncated": True,
                "original_bytes": size,
                "preview": json.dumps(payload, default=str)[:4096],
            }
            payload = truncated
        created.append(
            WorkerEvent(
                worker=worker,
                session=known.get(remote_session_id),
                remote_session_id=remote_session_id[:128],
                event_type=str(record.get("type") or "")[:64],
                payload=payload if isinstance(payload, dict) else {"value": payload},
                body_hash=body_hash,
            )
        )
    if created:
        WorkerEvent.objects.bulk_create(created, batch_size=200)
        prune_worker_event_ring(worker)
    return created


def prune_worker_event_ring(worker, cap=None):
    """Keep only the newest WORKER_EVENT_KEEP events per worker."""
    cap = cap or settings.WORKER_EVENT_KEEP
    excess = list(
        WorkerEvent.objects.filter(worker=worker).order_by("-pk").values_list("pk", flat=True)[cap:]
    )
    if excess:
        WorkerEvent.objects.filter(worker=worker, pk__in=excess).delete()
        return True
    return False


def protected_observation_ids():
    """Sessions survive retention while audit or pending interactions reference them.

    Open sessions' pending requests can belong to sessions older than the
    retention window (POC parity); they are pruned only once resolved
    snapshot-authoritatively.
    """
    protected = set()
    try:
        attempts = apps.get_model("coordinator", "TaskExecutionAttempt")
    except LookupError:
        attempts = None
    if attempts is not None:
        protected.update(
            attempts.objects.order_by().values_list("session_id", flat=True).distinct().iterator()
        )
    protected.update(
        PendingInteraction.objects.filter(resolved_at=None)
        .order_by()
        .values_list("session_id", flat=True)
        .distinct()
        .iterator()
    )
    return set(pk for pk in protected if pk)


@retry_sqlite
@transaction.atomic
def apply_observation_retention():
    """Bounded central cleanup; protects observation rows linked by audit records.
    - Messages: keep the newest WORKER_MESSAGE_KEEP rows per session.
    - Sessions: keep the newest WORKER_SESSION_KEEP per worker by last activity,
      skipping sessions that task execution attempts reference.
    - Events: per-worker ring of the newest WORKER_EVENT_KEEP rows.
    """
    removed = {"messages": 0, "sessions": 0, "events": 0}
    keep_messages = settings.WORKER_MESSAGE_KEEP
    keep_sessions = settings.WORKER_SESSION_KEEP
    protected = protected_observation_ids()
    for session in WorkerSession.objects.only("pk").iterator():
        excess = list(
            WorkerMessage.objects.filter(session=session)
            .order_by("-remote_time", "-pk")
            .values_list("pk", flat=True)[keep_messages:]
        )
        if excess:
            count, _ = WorkerMessage.objects.filter(session=session, pk__in=excess).delete()
            removed["messages"] += count
    for worker in Worker.objects.only("pk").iterator():
        candidates = [
            pk
            for pk in WorkerSession.objects.filter(worker=worker)
            .order_by("-last_seen_at", "-pk")
            .values_list("pk", flat=True)
            if pk not in protected
        ]
        if len(candidates) > keep_sessions:
            count, _ = WorkerSession.objects.filter(pk__in=candidates[keep_sessions:]).delete()
            removed["sessions"] += count
        if prune_worker_event_ring(worker):
            removed["events"] += 1
    return removed


# --- Worker sync application (issues #7/#11) --------------------------------


def sync_epoch():
    row, _created = ServerState.objects.get_or_create(pk=1, defaults={"pk": 1})
    return str(row.sync_epoch)


@retry_sqlite
@transaction.atomic
def regenerate_sync_epoch():
    """Explicit global resync trigger: bridges republish full snapshots next sync."""
    row, _created = ServerState.objects.get_or_create(pk=1, defaults={"pk": 1})
    row.sync_epoch = uuid.uuid4()
    row.save(update_fields=["sync_epoch"])
    return sync_epoch()


def apply_observed_session_states(worker, events):
    """Derive per-session execution status from received events.

    Error events persist the last execution error; a busy execution-status event
    marks a new execution and clears the previous error (documented rule #9/#10).
    Unknown sessions in the event stream are ignored (sessions are reported in
    the same batch's session inventory).
    """
    for event in events:
        session_id = event.get("sessionID") or ""
        if not session_id:
            continue
        session = WorkerSession.objects.filter(worker=worker, remote_id=session_id).first()
        if session is None:
            continue
        frame = event.get("payload")
        if not isinstance(frame, dict):
            continue
        properties = frame.get("payload") if isinstance(frame.get("payload"), dict) else frame
        properties = properties.get("properties") if isinstance(properties, dict) else {}
        properties = properties if isinstance(properties, dict) else {}
        event_type = event.get("type") or frame.get("type") or ""
        if event_type == "session.error":
            error = properties.get("error")
            if isinstance(error, dict):
                message = error.get("data", {}).get("message")
                message = message or json.dumps(error)[:1000]
            else:
                message = str(error) if error else json.dumps(properties)[:1000]
            record_execution_error(session, message)
        elif event_type == "session.status":
            status_value = properties.get("status")
            status_type = status_value.get("type") if isinstance(status_value, dict) else None
            if isinstance(status_type, str):
                session.execution_status = status_type[:32]
                session.save(update_fields=["execution_status", "updated_at"])
                if status_type == "busy":
                    clear_execution_error(session)


@retry_sqlite
@transaction.atomic
def apply_worker_sync(worker, intent):
    """Ingest one validated sync envelope transactionally and emit instructions.

    Atomicity: the whole batch commits together; any failing section (including a
    foreign session reference in updates) rejects the entire sync with no partial
    application. Unacknowledged sync retries passing the same cursor.batch_id have
    their event batch skipped (bounded buffers stay worker-side); sessions/meta
    snapshots and message upserts are monotonic and safe to re-apply. Command
    dispatch and pending-interaction integration are provided by follow-up issues
    (#12/#14) through the hooks below; the documented response shape is stable.
    """
    locked_worker = locked(Worker.objects.filter(pk=worker.pk)).first() or worker
    batch_id = intent["cursor"]["batch_id"] or ""
    retry_batch = bool(batch_id) and locked_worker.last_batch_id == batch_id
    record_worker_meta(locked_worker, intent["meta"])
    if intent["sessions"]:
        store_sessions(locked_worker, intent["sessions"])
    for update in intent["updates"]:
        try:
            store_messages(locked_worker, update["sessionID"], update["messages"])
        except TaskError as error:
            raise TaskError(
                f"Foreign or unknown session '{update['sessionID']}' in updates: {error.message}",
                400,
            ) from error
    if intent["events"] and not retry_batch:
        event_records = [
            {"type": event["type"], "sessionID": event["sessionID"], "payload": event["frame"]}
            for event in intent["events"]
        ]
        store_events(locked_worker, event_records)
        apply_observed_session_states(locked_worker, event_records)
        if batch_id:
            locked_worker.last_batch_id = batch_id
            locked_worker.save(update_fields=["last_batch_id", "updated_at"])
    apply_command_results(locked_worker, intent["results"])
    pending_count = ingest_pending_interactions(locked_worker, intent["pending"])
    tick_planning_queues(locked_worker)
    response = {
        "protocol_version": intent["protocol_version"],
        "epoch": sync_epoch(),
        "snapshot_epoch": sync_epoch(),
        "server_time": timezone.now(),
        "acknowledged_batch_id": batch_id or None,
        "event_sequence": sync.event_sequence(),
        "resync_required": False,
        "commands": dispatch_queued_commands(locked_worker),
        "pending_applied": pending_count,
        "live": {"protocol_version": 1, "path": "/ws/worker/"},
        "limits": {
            "sessions": worker.session_limit,
            "messages": worker.message_limit,
            "events": getattr(settings, "WORKER_EVENT_KEEP", 5000),
        },
    }
    _notify_worker_sync(locked_worker)
    return response


def _notify_worker_sync(worker):
    transaction.on_commit(
        lambda: dispatch_notification("worker.synced", subject="workers", worker=worker),
        robust=True,
    )


# --- Remote commands (issues #12/#14 helpers) -------------------------------

MAX_PENDING_READ = 200

INTERACTION_ACTIONS = ("permission.reply", "question.reply", "question.reject")


def action_allowed_by_queue(action):
    """Allowlist: every documented action is accepted; interactions validate in #14."""
    return action in {choice for choice, _ in WorkerCommand.ACTION_CHOICES}


def validate_command_payload(action, payload):
    """Normalize/validate per-action payload; returns stored payload dict."""
    payload = payload if isinstance(payload, dict) else {}
    if not action_allowed_by_queue(action):
        raise TaskError(f"Unknown command action '{action}'", 400)
    if action in INTERACTION_ACTIONS:
        request_id = str(payload.get("requestID") or payload.get("request_id") or "")
        if not request_id or len(request_id) > 128:
            raise TaskError(f"{action} requires the pending requestID", 400)
    stored = {}
    if action == "create":
        title = payload.get("title")
        if title is not None:
            stored["title"] = str(title)[:500]
        directory = payload.get("directory")
        if directory is not None:
            stored["directory"] = str(directory)[:500]
        model = payload.get("model")
        if model is not None:
            stored["model"] = _validate_model(model)
    elif action == "prompt":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TaskError("prompt requires non-empty text", 400)
        stored["text"] = text
        if payload.get("noReply") is not None:
            stored["noReply"] = bool(payload["noReply"])
        agent = payload.get("agent")
        if agent is not None:
            if not isinstance(agent, str) or not agent.strip():
                raise TaskError("agent must be a non-empty string", 400)
            stored["agent"] = agent.strip()[:100]
        model = payload.get("model")
        if model is not None:
            stored["model"] = _validate_model(model)
    return stored


def _validate_model(model):
    if not isinstance(model, dict) or not model.get("providerID") or not model.get("modelID"):
        raise TaskError("model must carry providerID and modelID", 400)
    return {"providerID": str(model["providerID"])[:128], "modelID": str(model["modelID"])[:128]}


def _sanitize_agent_catalog(value):
    """Bound and normalize the bridge-observed OpenCode agent list."""
    if not isinstance(value, list):
        return None
    agents = []
    for item in value[:200]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        agent = {"name": name.strip()[:100]}
        description = item.get("description")
        if isinstance(description, str) and description:
            agent["description"] = description[:500]
        if item.get("mode") in {"primary", "subagent", "all"}:
            agent["mode"] = item["mode"]
        if isinstance(item.get("builtIn"), bool):
            agent["builtIn"] = item["builtIn"]
        model = item.get("model")
        if isinstance(model, dict) and model.get("providerID") and model.get("modelID"):
            agent["model"] = _validate_model(model)
        agents.append(agent)
    return agents


def _agent_allowed(worker, agent):
    if not agent:
        return True
    catalog = worker.agent_catalog or []
    if not catalog:
        # No catalog observed yet; unknown worker-facing agents are forwarded as-is.
        return True
    return any(isinstance(row, dict) and row.get("name") == agent for row in catalog)


def _model_allowed(worker, model):
    if not model:
        return True
    catalog = worker.model_catalog or []
    if not catalog:
        # No catalog observed yet; unknown worker-facing models are forwarded as-is.
        return True
    for provider in catalog:
        if not isinstance(provider, dict):
            continue
        nested = provider.get("models")
        if isinstance(nested, dict):
            # Raw OpenCode /provider rows: {id, models: {modelID: {...}}}
            if provider.get("id") != model["providerID"]:
                continue
            for item in nested.values() or []:
                if item.get("id") == model["modelID"] and item.get("connectable", True):
                    return True
        else:
            # Compressed observation cards: {providerID, modelID}
            if (
                provider.get("providerID") == model["providerID"]
                and provider.get("modelID") == model["modelID"]
            ):
                return True
    return False


def _result_json(result):
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {"value": result}


def command_fingerprint(action, remote_session_id, payload):
    digest = hashlib.sha256()
    digest.update(json.dumps([action, remote_session_id or "", payload], sort_keys=True).encode())
    return digest.hexdigest()


def _iso(value):
    return value.isoformat() if value is not None else None


def worker_command_card(command, detail=False):
    """The machine/scope-safe command presentation for APIs and sync dispatch."""
    card = {
        "id": str(command.pk),
        "worker": command.worker.name,
        "action": command.action,
        "payload": command.payload,
        "sessionID": command.remote_session_id or None,
        "requestID": command.request_id or None,
        "status": command.status,
        "deliveryUncertain": command.delivery_uncertain,
        "createdAt": _iso(command.created_at),
    }
    if command.dispatched_at:
        card["dispatchedAt"] = _iso(command.dispatched_at)
    if command.completed_at:
        card["completedAt"] = _iso(command.completed_at)
    if detail:
        card["result"] = command.result
        if command.error:
            card["error"] = command.error
        card["createdBy"] = command.actor_label or (
            command.created_by.name if command.created_by else ""
        )
        card["clientRequestId"] = command.client_request_id or None
        card["deliveryUncertain"] = command.delivery_uncertain
        card["uncertain"] = command.uncertain_text if command.uncertain_text else None
    return card


@retry_sqlite
@transaction.atomic
def enqueue_worker_command(
    actor_agent,
    actor_label,
    worker,
    action,
    payload,
    remote_session_id="",
    client_request_id="",
    actor_user=None,
):
    """Durable, validated, idempotent command enqueue (issues #12).

    Rejections: unknown/disabled worker 404/403, offline or OpenCode-disconnected
    target 409, unknown session 404 (wrong-worker cannot be distinguished from
    unknown), model not in the worker's connected catalog 400, full queue 429,
    client_request_id replay with different payload 409 and identical replay
    returns the original card with 202 (idempotent create).
    """
    if worker.agent is None or not worker.agent.is_active:
        raise TaskError("Unknown worker", 404)
    if not worker.is_enabled:
        raise TaskError("Worker is not enabled for commands", 403)
    if not worker.is_online() or not worker.connected or not worker.stream_connected:
        raise TaskError("Worker or OpenCode is offline", 409)
    if not action_allowed_by_queue(action):
        raise TaskError(f"Unknown command action '{action}'", 400)
    stored_payload = validate_command_payload(action, payload)
    remote_session_id = (remote_session_id or "")[:128]
    request_id = ""
    interaction = None
    if action != "create":
        if not remote_session_id:
            raise TaskError(f"{action} requires a sessionID", 400)
        session = WorkerSession.objects.filter(worker=worker, remote_id=remote_session_id).first()
        if session is None:
            raise TaskError("Unknown session", 404)
    else:
        session = None
        if remote_session_id:
            raise TaskError("create commands do not carry a sessionID", 400)
    if action in INTERACTION_ACTIONS:
        request_id = str(payload.get("requestID") or payload.get("request_id") or "")
        interaction = resolve_pending_reply_targets(worker, action, request_id, remote_session_id)
        if action == "permission.reply":
            stored_payload = _validate_permission_reply(payload)
        elif action == "question.reply":
            stored_payload = _validate_question_reply(interaction, payload)
        else:
            stored_payload = {}
    if "agent" in stored_payload and not _agent_allowed(worker, stored_payload["agent"]):
        raise TaskError("Agent is not in this worker's connected agent catalog", 400)
    if "model" in stored_payload and not _model_allowed(worker, stored_payload["model"]):
        raise TaskError("Model is not in this worker's connected model catalog", 400)
    # Absent ids persist as SQL NULL so empty-string ids do not collide under
    # the (created_by, client_request_id) uniqueness across repeated commands.
    client_request_id = (client_request_id or "")[:64] or None
    fingerprint = command_fingerprint(action, remote_session_id, stored_payload)
    if client_request_id:
        matches = WorkerCommand.objects.filter(
            client_request_id__isnull=False, client_request_id=client_request_id
        )
        if actor_agent:
            existing = matches.filter(created_by=actor_agent).first()
        else:
            existing = matches.filter(created_by=None, actor_label=actor_label).first()
        if existing is not None:
            if existing.payload_fingerprint != fingerprint:
                raise TaskError("client_request_id matches a different command", 409)
            return existing, False
    cap = settings.WORKER_COMMAND_QUEUE_CAP
    if WorkerCommand.objects.filter(worker=worker, status="queued").count() >= cap:
        raise TaskError(f"Command queue is full ({cap} queued)", 429)
    command = WorkerCommand.objects.create(
        worker=worker,
        created_by=actor_agent,
        actor_label=actor_label or (actor_agent.name if actor_agent else ""),
        # Direct human commands name their user; launches pass the launching human.
        actor_user=actor_user or (human_user(actor_label) if actor_agent is None else None),
        action=action,
        payload=stored_payload,
        remote_session_id=remote_session_id,
        session=session,
        request_id=request_id,
        client_request_id=client_request_id,
        payload_fingerprint=fingerprint,
    )
    if interaction is not None:
        interaction.reply_command = command
        interaction.save(update_fields=["reply_command"])
    store_events(
        worker,
        [
            {
                "type": "command.queued",
                "sessionID": command.remote_session_id or None,
                "payload": {"type": "command.queued", "properties": worker_command_card(command)},
            }
        ],
    )

    def notify():
        dispatch_notification("command.queued", subject="workers", worker=worker, command=command)

    transaction.on_commit(notify, robust=True)
    return command, True


def dispatch_queued_commands(worker):
    """Atomically dispatch queued commands inside the worker-locked sync (#11/#12).

    Each queued row transitions exactly once: the update filters status='queued'
    and the same-worker sync serialization guarantees no double delivery.
    """
    cap = 200
    ids = list(
        WorkerCommand.objects.filter(worker=worker, status="queued")
        .order_by("pk")
        .values_list("pk", flat=True)[:cap]
    )
    if not ids:
        return []
    now = timezone.now()
    WorkerCommand.objects.filter(pk__in=ids).update(status="dispatched", dispatched_at=now)
    store_events(
        worker,
        [
            {
                "type": "command.dispatched",
                "sessionID": None,
                "payload": {"type": "command.dispatched", "properties": {"ids": ids}},
            }
        ],
    )
    dispatched = WorkerCommand.objects.filter(pk__in=ids).select_related("worker", "created_by")
    return [worker_command_card(command, detail=True) for command in dispatched]


def classify_command_result(result, existing):
    """Map a bridge result to (status, delivery_uncertain, uncertain_text)."""
    status = str(result.get("status") or "").lower()
    error_text = ""
    error = result.get("error")
    if error is not None:
        error_text = error if isinstance(error, str) else json.dumps(error)[:5000]
    if status == "uncertain" or (
        error_text and "uncertain" in error_text.lower() and status == "error"
    ):
        return "dispatched", True, (error_text or "Delivery uncertain; reconcile with the worker.")
    if status not in ("done", "error"):
        return None, False, ""
    return status, False, error_text


@retry_sqlite
@transaction.atomic
def apply_command_results(worker, results):
    """Ingest command results from the sync envelope (issues #12).

    Foreign or unknown results are ignored so a whole batch never fails on
    command bookkeeping; terminal commands absorb repeated identical uploads
    idempotently and skip conflicting replays; a best-effort bounded enqueue of
    the WorkerCommand row itself is retained on the worker (cap check is ours).
    """
    outcome = {"applied": 0, "ignored": 0, "uncertain": 0}
    if not isinstance(results, list):
        return outcome
    for entry in results:
        if not isinstance(entry, dict):
            outcome["ignored"] += 1
            continue
        raw_id = str(entry.get("commandID") or entry.get("id") or "")
        if not raw_id.isdigit():
            outcome["ignored"] += 1
            continue
        command = WorkerCommand.objects.filter(worker=worker, pk=int(raw_id)).first()
        if command is None:
            outcome["ignored"] += 1
            continue
        uncertain_other = classify_command_result(entry, command)[1]
        if command.status in ("done", "error"):
            if command.delivery_uncertain and uncertain_other:
                command.delivery_uncertain = False
                command.uncertain_text = ""
                command.save(update_fields=["delivery_uncertain", "uncertain_text"])
            outcome["ignored"] += 1
            continue
        status, uncertain, error_text = classify_command_result(entry, command)
        if status is None:
            outcome["ignored"] += 1
            continue
        result_payload = entry.get("result")
        if uncertain:
            command.status = "dispatched"
            command.delivery_uncertain = True
            command.uncertain_text = error_text
            command.error = ""
            command.save(update_fields=["status", "delivery_uncertain", "uncertain_text", "error"])
            event_type = "command.uncertain"
            outcome["uncertain"] += 1
        else:
            command.status = status
            command.delivery_uncertain = False
            command.uncertain_text = ""
            command.result = _result_json(result_payload)
            command.error = error_text if status == "error" else ""
            command.completed_at = timezone.now()
            command.save(
                update_fields=[
                    "status",
                    "delivery_uncertain",
                    "uncertain_text",
                    "result",
                    "error",
                    "completed_at",
                ]
            )
            event_type = f"command.{status}"
            outcome["applied"] += 1
        store_events(
            worker,
            [
                {
                    "type": event_type,
                    "sessionID": command.remote_session_id or None,
                    "payload": {"type": event_type, "properties": worker_command_card(command)},
                }
            ],
        )

        command_ref = command
        worker_ref = worker

        # #13 correlation: create-command outcomes map to attempt evidence
        # (accepted | error | uncertain); prompt commands link as evidence.
        if command.action == "create":
            _correlate_create_outcome(
                command,
                "uncertain" if uncertain else command.status,
                command.result or {},
                command.error,
                command.delivery_uncertain,
            )
        elif command.action == "prompt":
            _link_prompt_command(command)

        def notify(
            command=command_ref,
            worker=worker_ref,
            transition_type=event_type,
        ):
            dispatch_notification(
                transition_type, subject="workers", worker=worker, command=command
            )

        transaction.on_commit(notify, robust=True)
    return outcome


@retry_sqlite
@transaction.atomic
def resolve_worker_command(command, status, result=None, error=""):
    """Explicit operator reconciliation for queued/dispatched/uncertain commands."""
    if status not in ("done", "error"):
        raise TaskError("Resolution status must be done or error", 400)
    if command.status in ("done", "error"):
        raise TaskError("Command already resolved", 409)
    command.status = status
    command.result = _result_json(result)
    command.error = str(error or "")[:5000]
    command.completed_at = timezone.now()
    command.save(update_fields=["status", "result", "error", "completed_at"])
    event_type = f"command.{status}"
    store_events(
        command.worker,
        [
            {
                "type": event_type,
                "sessionID": command.remote_session_id or None,
                "payload": {"type": event_type, "properties": worker_command_card(command)},
            }
        ],
    )
    transaction.on_commit(
        lambda: dispatch_notification(
            event_type, subject="workers", worker=command.worker, command=command
        ),
        robust=True,
    )
    return command


# --- Pending interactions (issues #14) --------------------------------------

PERMISSION_REPLY_CHOICES = ("once", "always", "reject")


def interaction_question_specs(data):
    """The ordered question specs for question-kind interactions."""
    questions = data.get("questions") if isinstance(data, dict) else None
    return questions if isinstance(questions, list) else []


def interaction_card(interaction):
    """A worker/pending read presentation without exposing worker internals."""
    data = interaction.data if isinstance(interaction.data, dict) else {}
    card = {
        "id": interaction.request_id,
        "kind": interaction.kind,
        "promptType": handoff.classify_prompt(interaction.kind, data),
        "sessionID": interaction.remote_session_id or None,
        "lastSeenAt": _iso(interaction.last_seen_at) or _iso(interaction.first_seen_at),
        "replyDispatched": bool(interaction.reply_command_id),
    }
    if interaction.reply_command_id:
        card["replyCommandId"] = str(interaction.reply_command_id)
    if interaction.kind == "permission":
        for name in ("type", "title", "description", "patterns"):
            if data.get(name) is not None:
                card[name] = data[name]
    else:
        card["questions"] = [
            {
                "header": q.get("header") or "",
                "text": q.get("text") or "",
                "options": [
                    {"label": o.get("label") or "", "description": o.get("description") or ""}
                    for o in (q.get("options") or [])
                ],
                "multiple": bool(q.get("multiple")),
                "custom": q.get("custom") is not False,
            }
            for q in interaction_question_specs(interaction.data)
        ]
    return card


def _interaction_record(kind, record):
    """Normalize one bridge pending record (POC permission/question rows)."""
    request_id = str(record.get("id") or record.get("requestID") or "")
    session_id = str(record.get("sessionID") or "")
    if not record or not request_id or len(request_id) > 128:
        raise TaskError("Pending interaction is missing an id", 400)
    if kind == "question" and session_id and not record.get("questions"):
        raise TaskError("Question requests require the ordered questions array", 400)
    return {"request_id": request_id[:128], "session_id": session_id[:128], "data": record}


def pending_interaction_key(kind):
    """Sync pending key → interaction kind; unknown kinds are skipped upstream."""
    return "permission" if kind == "permissions" else "question" if kind == "questions" else None


@retry_sqlite
@transaction.atomic
def ingest_pending_interactions(worker, pending):
    """Authoritative pending-interaction snapshot handling (issues #14).

    A `complete` true snapshot upserts every request and resolves (marks) all
    previously-seen but now-absent requests — the only path that disappears or
    resolves central rows. An incomplete snapshot (bridge fetch failed) changes
    nothing but is recorded as an explicit bounded gap. New requests from an
    incomplete batch are still not trusted to remove anything else.
    """
    outcome = {
        "complete": bool(pending.get("complete")),
        "added": 0,
        "seen": 0,
        "resolved": 0,
        "skipped": 0,
    }
    now = timezone.now()
    records_by_kind = {}
    for key, kind in (("permissions", "permission"), ("questions", "question")):
        seen = []
        for record in pending.get(key) or []:
            if not isinstance(record, dict):
                outcome["skipped"] += 1
                continue
            try:
                seen.append(_interaction_record(kind, record))
            except TaskError:
                outcome["skipped"] += 1
                continue
        records_by_kind[kind] = seen
    if outcome["complete"] is False and not (
        records_by_kind["permission"] or records_by_kind["question"]
    ):
        return outcome
    keep = set()
    for kind, records in records_by_kind.items():
        for record in records:
            session = None
            if record["session_id"]:
                session = WorkerSession.objects.filter(
                    worker=worker, remote_id=record["session_id"]
                ).first()
            interaction, created = PendingInteraction.objects.get_or_create(
                worker=worker,
                kind=kind,
                request_id=record["request_id"],
                defaults={
                    "remote_session_id": record["session_id"],
                    "session": session,
                    "data": record["data"],
                    "last_seen_at": now,
                },
            )
            if created:
                outcome["added"] += 1
            if not created:
                changed = [
                    (name, value)
                    for name, value in (
                        ("data", record["data"]),
                        ("remote_session_id", record["session_id"]),
                    )
                    if getattr(interaction, name) != value
                ]
                for name, value in changed:
                    setattr(interaction, name, value)
            if session and interaction.session_id != session.pk:
                interaction.session = session
            reopened = bool(interaction.resolved_at and outcome["complete"])
            if reopened:
                # A complete snapshot re-observing a request re-opens it.
                interaction.resolved_at = None
                interaction.resolution = ""
            interaction.last_seen_at = now
            interaction.save()
            if created or reopened:
                maybe_auto_answer(worker, interaction)
            keep.add((kind, interaction.request_id))
            outcome["seen"] += 1
    if outcome["complete"]:
        open_rows = PendingInteraction.objects.filter(worker=worker, resolved_at=None)
        for interaction in open_rows:
            key = (interaction.kind, interaction.request_id)
            if key in keep:
                continue
            interaction.resolved_at = now
            interaction.resolution = "snapshot"
            interaction.save(update_fields=["resolved_at", "resolution"])
            store_events(
                worker,
                [
                    {
                        "type": "interaction.resolved",
                        "sessionID": interaction.remote_session_id or None,
                        "payload": {
                            "type": "interaction.resolved",
                            "properties": interaction_card(interaction),
                        },
                    }
                ],
            )
            outcome["resolved"] += 1
            _notify_interaction(worker, "interaction.resolved", interaction)
    # Late link: pending request rows adopt their observed session row.
    for interaction in PendingInteraction.objects.filter(
        worker=worker, resolved_at=None, session=None
    ).exclude(remote_session_id=""):
        session = WorkerSession.objects.filter(
            worker=worker, remote_id=interaction.remote_session_id
        ).first()
        if session:
            interaction.session = session
            interaction.save(update_fields=["session"])
    return outcome


def handoff_task_for(worker, interaction):
    """The semi-automatic task whose live attempt owns this prompt's session, if any."""
    if not interaction.remote_session_id:
        return None
    attempt = (
        TaskExecutionAttempt.objects.filter(
            worker=worker,
            remote_session_id=interaction.remote_session_id,
            outcome__in=[
                TaskExecutionAttempt.Outcome.PENDING,
                TaskExecutionAttempt.Outcome.ACCEPTED,
            ],
            task__handoff_mode=Task.HandoffMode.SEMI_AUTO,
        )
        .select_related("task")
        .first()
    )
    return attempt.task if attempt else None


def maybe_auto_answer(worker, interaction):
    """Hand-off mode (#431): auto-approve binary prompts, escalate everything else.

    Reuses the normal permission.reply command path, so ownership, duplicate and
    queue checks still apply. Any failure leaves the prompt pending for a human.
    Every decision is written to the session stream; approvals also to the task
    audit trail.
    """
    task = handoff_task_for(worker, interaction)
    if task is None or interaction.reply_command_id:
        return None
    prompt_type, action, rule = handoff.decide(interaction.kind, interaction.data)
    decision = "once" if action == "approve" else "escalated"
    if action == "approve":
        try:
            with transaction.atomic():
                enqueue_worker_command(
                    None,
                    "handoff",
                    worker,
                    "permission.reply",
                    {"requestID": interaction.request_id, "reply": "once"},
                    remote_session_id=interaction.remote_session_id,
                    client_request_id=f"handoff-{interaction.pk}",
                )
        except TaskError as exc:
            decision, rule = "escalated", rule or f"enqueue failed: {exc}"
    audit = {
        "taskId": task.pk,
        "sessionID": interaction.remote_session_id,
        "requestID": interaction.request_id,
        "promptType": prompt_type,
        "decision": decision,
        "rule": rule,
        "at": _iso(timezone.now()),
    }
    event_type = "handoff.auto_answered" if decision == "once" else "handoff.escalated"
    store_events(
        worker,
        [
            {
                "type": event_type,
                "sessionID": interaction.remote_session_id or None,
                "payload": {"type": event_type, "properties": audit},
            }
        ],
    )
    if decision == "once":
        task.log(
            TaskEvent.Type.HANDOFF_AUTO_ANSWER,
            None,
            f"Auto-approved {prompt_type} prompt {interaction.request_id} "
            f"(session {interaction.remote_session_id}, decision {decision}, rule none)",
        )
    return audit


def _notify_interaction(worker, event_type, interaction):
    transaction.on_commit(
        lambda: dispatch_notification(
            event_type,
            subject="workers",
            worker=worker,
            session=interaction.session,
        ),
        robust=True,
    )


def resolve_pending_reply_targets(worker, action, request_id, remote_session_id):
    """Validate ownership/pending state of a reply before enqueue (#14)."""
    kind = {
        "permission.reply": "permission",
        "question.reply": "question",
        "question.reject": "question",
    }[action]
    lookup = PendingInteraction.objects.select_for_update().filter(
        worker=worker, kind=kind, request_id=request_id
    )
    interaction = lookup.first()
    if interaction is None or interaction.resolved_at is not None:
        raise TaskError("Request is no longer pending on this session", 404)
    if (interaction.remote_session_id or "") != (remote_session_id or ""):
        raise TaskError("Request is no longer pending on this session", 404)
    duplicate = WorkerCommand.objects.filter(
        worker=worker,
        action__in=INTERACTION_ACTIONS,
        request_id=request_id,
        status__in=("queued", "dispatched"),
    ).exists()
    answered = (
        WorkerCommand.objects.filter(
            worker=worker, action__in=INTERACTION_ACTIONS, request_id=request_id, status="done"
        )
        .exclude(result__value={"answer": [], "reply": "reject"})
        .exists()
    )
    if duplicate or answered:
        raise TaskError("A response is already being delivered", 409)
    return interaction


def _validate_permission_reply(payload):
    reply = payload.get("reply")
    if reply not in PERMISSION_REPLY_CHOICES:
        raise TaskError("Invalid permission response", 400)
    stored = {"reply": reply}
    message = payload.get("message")
    if message is not None:
        stored["message"] = str(message)[:2000]
    return stored


def _validate_question_reply(interaction, payload):
    specs = interaction_question_specs(interaction.data)
    answers = payload.get("answers")
    generic = "Answer every question using its allowed choices or a custom response"
    if (
        not isinstance(answers, list)
        or len(answers) != len(specs)
        or not all(isinstance(answer, list) and answer for answer in answers)
        or not all(
            isinstance(value, str) and value.strip() for answer in answers for value in answer
        )
    ):
        raise TaskError(generic, 400)
    for index, spec in enumerate(specs):
        answer = answers[index]
        allowed = None
        if spec.get("custom") is False:
            allowed = {(option or {}).get("label") for option in spec.get("options") or []}
        for value in answer:
            if len(value) > 200:
                raise TaskError(generic, 400)
            if allowed is not None and value not in allowed:
                raise TaskError(generic, 400)
        if not spec.get("multiple") and len(answer) != 1:
            raise TaskError(generic, 400)
    return {"answers": answers}


# --- Task execution attempts (issues #13) ------------------------------------


def attempt_read_model(attempt):
    """Read attempt evidence without leaking worker internals."""
    return {
        "id": attempt.pk,
        "task": attempt.task_id,
        "owner": attempt.owner.name,
        "worker": attempt.worker.name,
        "directory": attempt.directory,
        "createCommandId": str(attempt.create_command_id) if attempt.create_command_id else None,
        "promptCommandId": str(attempt.prompt_command_id) if attempt.prompt_command_id else None,
        "remoteSessionId": attempt.remote_session_id or None,
        "session": attempt.session_id,
        "outcome": attempt.outcome,
        "outcomeDetail": attempt.outcome_detail,
        "launchedAt": _iso(attempt.launched_at),
        "resolvedAt": _iso(attempt.resolved_at),
    }


@retry_sqlite
@transaction.atomic
def launch_task_execution(
    actor_agent,
    actor_label,
    task_id,
    worker,
    directory="",
    client_request_id="",
    title="",
    model=None,
):
    """Authorized launcher: reuse the atomic claim rules; never bypass ownership.

    The task must be claimed (claim owner = actor agent, or staff actor naming
    the task's claim owner). The launch queues a create command via the #12
    service and records ONE pending attempt; retrying with the same
    client_request_id returns the existing attempt/command pair without
    creating hidden duplicates, and a launch with a pending active attempt
    conflicts (409) until that attempt resolves. Task status moves only
    through the existing human lifecycle (review/done rules stay unchanged).
    """
    task = get_locked(task_id, skip_locked=True)
    if task.claimed_by is None:
        raise TaskError("Launch requires the task to be claimed by its owner", 409)
    owner = task.claimed_by
    if actor_agent is not None and owner.pk != actor_agent.pk:
        raise TaskError("Only the task owner launches execution attempts", 403)
    if actor_agent is None and actor_label:
        pass  # staff launches the claim owner's attempt after verifying identity
    if not owner.is_active:
        raise TaskError("Claim owner agent is inactive", 409)
    if worker is None:
        raise TaskError("Unknown worker", 404)
    if worker.agent_id != owner.pk:
        raise TaskError("Launch requires the worker bound to the task owner", 409)
    directory = (directory or "").strip()[:500] or worker.create_directory or worker.directory
    pending = TaskExecutionAttempt.objects.filter(
        task=task, outcome=TaskExecutionAttempt.Outcome.PENDING
    ).first()
    if pending:
        create_card = None
        create_command = pending.create_command
        if (
            create_command
            and client_request_id
            and create_command.client_request_id == client_request_id
        ):
            create_card = worker_command_card(pending.create_command, detail=True)
        raise TaskError(
            {
                "detail": "An execution attempt is already active; resolve it first",
                "attempt": attempt_read_model(pending),
                "createCommand": create_card,
            },
            409,
        )
    create_request_id = client_request_id or f"task-{task.pk}-launch"
    launcher = human_user(actor_label) if actor_agent is None else None
    payload = {"directory": directory}
    if title:
        payload["title"] = title
    if model:
        payload["model"] = model
    command, _created = enqueue_worker_command(
        owner,
        actor_label or owner.name,
        worker,
        "create",
        payload,
        remote_session_id="",
        client_request_id=create_request_id,
        actor_user=launcher,
    )
    attempt = TaskExecutionAttempt.objects.create(
        task=task,
        owner=owner,
        worker=worker,
        directory=directory,
        create_command=command,
        remote_session_id="",
        session=None,
        outcome=TaskExecutionAttempt.Outcome.PENDING,
    )
    log_human(
        task,
        TaskEvent.Type.EXECUTION_LAUNCHED,
        owner,
        f"Execution attempt {attempt.pk} on {worker.name} ({directory}); command {command.pk}",
        actor_label if actor_agent is None else None,
    )
    transaction.on_commit(
        lambda: dispatch_notification(
            TaskEvent.Type.EXECUTION_LAUNCHED,
            subject="tasks",
            worker=worker,
            command=command,
        ),
        robust=True,
    )
    return attempt, command, True


def _correlate_create_outcome(command, status, result_payload, error_text, uncertain):
    """Task status stays human-controlled; attempts record evidence per #13."""
    attempt = TaskExecutionAttempt.objects.filter(create_command_id=command.pk).first()
    if attempt is None or attempt.outcome != TaskExecutionAttempt.Outcome.PENDING:
        return None
    detail = {
        "commandId": command.pk,
        "status": command.status,
        "reported": status,
    }
    if status == "done":
        remote = result_payload.get("id") if isinstance(result_payload, dict) else None
        if isinstance(remote, str) and remote:
            attempt.remote_session_id = remote[:128]
        attempt.outcome = TaskExecutionAttempt.Outcome.ACCEPTED
        detail["accept"] = "create command acknowledged by the worker"
    elif uncertain:
        attempt.outcome = TaskExecutionAttempt.Outcome.UNCERTAIN
        detail["uncertain"] = error_text or "create delivery uncertain"
    else:
        attempt.outcome = TaskExecutionAttempt.Outcome.ERROR
        detail["error"] = error_text or "create command failed"
    attempt.outcome_detail = json.dumps(detail)[:5000]
    attempt.resolved_at = timezone.now()
    attempt.save(update_fields=["outcome", "outcome_detail", "resolved_at", "remote_session_id"])
    attempt.task.log(
        TaskEvent.Type.EXECUTION_DONE,
        attempt.owner,
        f"Attempt {attempt.pk} outcome {attempt.outcome}: {attempt.outcome_detail[:300]}",
    )
    transaction.on_commit(
        lambda: dispatch_notification("execution.attempt", subject="tasks", worker=attempt.worker),
        robust=True,
    )
    return attempt


def _link_prompt_command(command):
    """A prompt command targeting the attempt's session links as evidence (#13)."""
    if not command.remote_session_id:
        return None
    attempt = TaskExecutionAttempt.objects.filter(
        worker=command.worker,
        remote_session_id=command.remote_session_id,
        outcome__in=[
            TaskExecutionAttempt.Outcome.PENDING,
            TaskExecutionAttempt.Outcome.ACCEPTED,
        ],
    ).first()
    if attempt is None:
        return None
    attempt.prompt_command = command
    attempt.save(update_fields=["prompt_command"])
    return attempt


@retry_sqlite
@transaction.atomic
def link_attempt_session(attempt, session):
    """Explicit, authorizedsession link (agent owner or staff validation)."""
    if session is None or session.worker_id != attempt.worker_id:
        raise TaskError("Unknown session for this attempt's worker", 404)
    attempt.session = session
    if not attempt.remote_session_id:
        attempt.remote_session_id = session.remote_id
    attempt.save(update_fields=["session", "remote_session_id"])
    attempt.task.log(
        TaskEvent.Type.EXECUTION_LINKED,
        attempt.owner,
        f"Attempt {attempt.pk} linked session {session.remote_id}",
    )
    transaction.on_commit(
        lambda: dispatch_notification(
            TaskEvent.Type.EXECUTION_LINKED,
            subject="tasks",
            worker=attempt.worker,
            session=session,
        ),
        robust=True,
    )
    return attempt


def session_attempt_links(session):
    """Task links for session-oriented read models (#15 wiring helper)."""
    links = session.execution_attempts.select_related("task", "worker", "owner")
    return [attempt_read_model(attempt) for attempt in links.order_by("-id")]


# --- Unified worker/session read APIs (issue #15) ---------------------------

MAX_SESSION_COMMANDS = 50
MAX_SESSION_SUBAGENTS = 100
MAX_MESSAGE_READ = 500
DEFAULT_MESSAGE_LIMIT = 100
MAX_EVENT_READ = 500
DEFAULT_EVENT_LIMIT = 200


def session_card(session, detail=False):
    """Credential-free worker-scoped session read model (#15).

    The parent is the worker-scoped row resolved from remote_parent_id;
    a missing parent row keeps the remote ID for refetch-and-correlate.
    """
    card = {
        "sessionID": session.remote_id,
        "workerId": session.worker_id,
        "worker": session.worker.name,
        "directory": session.directory,
        "title": session.title,
        "agentName": session.agent_name,
        "model": session.model,
        "parentID": session.remote_parent_id or None,
        "parent": session.parent.remote_id if session.parent_id else None,
        "executionStatus": session.execution_status,
        "lastError": session.last_error or None,
        "lastErrorAt": _iso(session.last_error_at),
        "remoteCreatedMs": session.remote_created_ms,
        "remoteUpdatedMs": session.remote_updated_ms,
        "firstSeenAt": _iso(session.first_seen_at),
        "lastSeenAt": _iso(session.last_seen_at),
        # Window completeness (#9): absent messages are retained/gap metadata,
        # never silently deletions; OpenCode stays the canonical history owner.
        "window": {
            "count": session.window_count,
            "oldestId": session.window_oldest_id or None,
            "complete": session.window_complete,
        },
        "counts": {
            "messages": session.messages.count(),
            "events": session.events.count(),
            "pending": session.pending_interactions.filter(resolved_at=None).count(),
        },
    }
    if detail:
        card["subagents"] = [
            other.remote_id
            for other in session.subagents.all().order_by("-id")[:MAX_SESSION_SUBAGENTS]
        ]
    return card


def session_detail(session, compact=False):
    """Full session read model: health, commands/results, interactions, task links."""
    card = session_card(session, detail=True)
    card["pendingInteractions"] = [
        interaction_card(interaction)
        for interaction in session.pending_interactions.filter(resolved_at=None).order_by("-id")[
            :MAX_PENDING_READ
        ]
    ]
    commands = session.worker.commands.filter(remote_session_id=session.remote_id).order_by("-id")
    if compact:
        reply_ids = [request.get("replyCommandId") for request in card["pendingInteractions"]]
        commands = commands.filter(pk__in=[value for value in reply_ids if value])
    card["commands"] = []
    for command in commands[:MAX_SESSION_COMMANDS]:
        command_card = worker_command_card(command, detail=not compact)
        if compact:
            command_card["payload"] = {}
        card["commands"].append(command_card)
    attempts = session_attempt_links(session)
    card["attempts"] = attempts
    card["taskLinks"] = [attempt["task"] for attempt in attempts]
    return card


def message_card(message):
    """One upserted message: bounded server window, OpenCode remains canonical."""
    return {
        "id": message.pk,
        "remoteID": message.remote_id,
        "role": message.role,
        "remoteTime": message.remote_time,
        "publishedAt": _iso(message.published_at),
        "content": message.content,
        "parts": message.parts,
    }


def recent_messages_read(session, before=None, limit=10):
    """Latest-first pages on the wire boundary, chronological messages for display.

    PK alone is not a timeline: a reconciliation may insert older messages after
    newer ones. The exclusive cursor retains both remote creation time and PK.
    """
    try:
        limit = min(max(int(limit), 1), 10)
        cursor = tuple(int(value) for value in before.split(":")) if before else None
        if cursor and (len(cursor) != 2 or cursor[1] < 1):
            raise ValueError
        if cursor and not (-(2**63) <= cursor[0] < 2**63 and cursor[1] < 2**63):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise TaskError("Invalid history limit or before cursor", 400) from None
    rows = session.messages.annotate(
        timeline_time=Coalesce("remote_time", 0, output_field=BigIntegerField())
    )
    gap = bool(cursor and not rows.filter(pk=cursor[1]).exists())
    if cursor:
        rows = rows.filter(
            Q(timeline_time__lt=cursor[0]) | Q(timeline_time=cursor[0], pk__lt=cursor[1])
        )
    page = list(rows.order_by("-timeline_time", "-pk")[:limit])
    oldest = page[-1] if page else None
    has_more = (
        bool(oldest)
        and rows.filter(
            Q(timeline_time__lt=oldest.timeline_time)
            | Q(timeline_time=oldest.timeline_time, pk__lt=oldest.pk)
        ).exists()
    )
    return {
        "messages": [message_card(message) for message in reversed(page)],
        "cursor": page[0].pk if page else 0,
        "olderCursor": f"{oldest.timeline_time}:{oldest.pk}" if has_more else None,
        "gap": gap,
        "hasMore": has_more,
        "retentionLimit": settings.WORKER_MESSAGE_KEEP,
    }


def messages_read(session, after=None, limit=None):
    """Cursor read of a session's message window: stable pk order, gap-aware."""
    after = None if after is None else max(int(after), 0)
    if limit is None:
        limit = DEFAULT_MESSAGE_LIMIT
    limit = min(max(int(limit), 0), MAX_MESSAGE_READ)
    rows = session.messages.all()
    oldest_pk = rows.order_by("id").values_list("id", flat=True).first()
    gap = bool(after is not None and oldest_pk is not None and oldest_pk > after + 1)
    base = oldest_pk - 1 if gap else after
    if base is not None:
        rows = rows.filter(id__gt=base)
    page = list(rows.order_by("id")[:limit])
    cursor = page[-1].pk if page else (after or 0)
    return {
        "messages": [message_card(message) for message in page],
        "cursor": cursor,
        "gap": gap,
        "hasMore": bool(page) and rows.filter(id__gt=cursor).order_by("id").exists(),
        "retentionLimit": settings.WORKER_MESSAGE_KEEP,
    }


def event_card(event):
    """One durable worker observation with the stable global cursor."""
    return {
        "id": event.pk,
        "type": event.event_type,
        "sessionID": event.remote_session_id or None,
        "payload": event.payload,
        "receivedAt": _iso(event.received_at),
    }


def events_read(worker, after=None, limit=None, cap=None):
    """Cursor read of the per-worker event ring with gap/expiry markers (#15).

    `after` is the last observed event id (exclusive). When `after` predates
    the retained ring (events pruned or dropped), `gap=True` and the read
    restarts at the retained floor so clients refetch an authoritative window
    instead of imagining replay. dropped_events counts the server-side
    documented overflow, not silent loss.
    """
    after = None if after is None else max(int(after), 0)
    if cap is not None:  # physics of the bounded retention window
        cap = min(int(cap), settings.WORKER_EVENT_KEEP)
    if limit is None:
        limit = DEFAULT_EVENT_LIMIT
    limit = min(max(int(limit), 0), MAX_EVENT_READ)
    rows = worker.events.all()
    oldest_pk = rows.order_by("id").values_list("id", flat=True).first()
    gap = bool(after is not None and oldest_pk is not None and oldest_pk > after + 1)
    base = oldest_pk - 1 if gap else after
    if base is not None:
        rows = rows.filter(id__gt=base)
    page = list(rows.order_by("id")[:limit])
    cursor = page[-1].pk if page else (after or 0)
    return {
        "events": [event_card(event) for event in page],
        "cursor": cursor,
        "gap": gap,
        "hasMore": bool(page) and rows.filter(id__gt=cursor).order_by("id").exists(),
        "dropped": worker.dropped_events,
        "retentionLimit": cap or settings.WORKER_EVENT_KEEP,
    }


def filter_worker_sessions(sessions, directory="", status="", project_id=None):
    """Apply project/worker/session filters BEFORE limits (#15)."""
    if directory:
        sessions = sessions.filter(directory=directory)
    if status:
        sessions = sessions.filter(execution_status=status)
    if project_id:
        # Project association comes through #13 attempt links (worker + session row),
        # so a project read lists only attempts' observed sessions.
        sessions = sessions.filter(execution_attempts__task__project_id=project_id).distinct()
    return sessions
