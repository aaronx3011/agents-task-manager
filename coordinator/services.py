import hashlib
import json
import time
import uuid
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

from django.apps import apps
from django.conf import settings
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import BigIntegerField, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import sync
from .errors import TaskError
from .models import (
    Agent,
    PendingInteraction,
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


def retry_sqlite(function):
    """Retry a rolled-back deferred SQLite transaction; PostgreSQL uses row locks."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(6):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if connection.vendor != "sqlite" or "locked" not in str(exc).lower():
                    raise
                if attempt == 5:
                    raise TaskError("Database busy; retry the request", 503) from exc
                time.sleep(0.02 * (2**attempt))

    return wrapped


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
):
    dependencies = list(Task.objects.filter(pk__in=depends_on))
    if len(dependencies) != len(set(depends_on)):
        raise TaskError("One or more dependencies do not exist", 400)
    task = Task.objects.create(
        project=project,
        title=title,
        description=description,
        priority=priority,
        created_by=agent,
    )
    task.depends_on.set(dependencies)
    task.log(TaskEvent.Type.CREATED, agent, audit_message("Task created", human_actor))
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
    task.log(TaskEvent.Type.HEARTBEAT, agent, audit_message("", human_actor))
    return task


@retry_sqlite
@transaction.atomic
def update_status(agent, task_id, status, reason="", result_summary="", human_actor=None):
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
    task.log(
        TaskEvent.Type.STATUS_CHANGED,
        agent,
        audit_message(f"{previous} -> {status}: {reason or result_summary}", human_actor),
    )
    return task


def clear_claim(task, agent, reason):
    task.status = Task.Status.PENDING
    task.claimed_by = task.claimed_at = task.heartbeat_at = None
    task.blocked_reason = ""
    task.save()
    task.log(TaskEvent.Type.RELEASED, agent, reason)
    return task


@retry_sqlite
@transaction.atomic
def release_task(agent, task_id, reason="", human_actor=None):
    task = get_locked(task_id)
    require_owner(task, agent, human_actor)
    return clear_claim(task, agent, audit_message(reason, human_actor))


@retry_sqlite
@transaction.atomic
def add_note(agent, task_id, message, human_actor=None):
    task = get_locked(task_id)
    if task.claimed_by_id is not None:
        require_owner(task, agent, human_actor)
    task.save(update_fields=["updated_at"])
    task.log(TaskEvent.Type.NOTE, agent, audit_message(message, human_actor))
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
    editable = {"title", "description", "project", "priority", "depends_on"}
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
    task.log(
        TaskEvent.Type.NOTE,
        agent,
        audit_message("Updated " + ", ".join(sorted(changes)), human_actor),
    )
    return task


@transaction.atomic
def admin_save_task(instance, changed_fields, dependencies, username):
    """Apply only human-edited fields to current locked state, preserving live heartbeats."""
    creating = instance.pk is None
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
    if creating or "depends_on" in changed_fields:
        validate_dependencies(task.pk, dependencies)
        task.depends_on.set(dependencies)
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
    default_model = meta.get("default_model")
    if isinstance(default_model, dict):
        worker.default_model = default_model
    for name in ("session_limit", "message_limit", "dropped_events"):
        value = meta.get(name)
        if isinstance(value, int) and 0 <= value <= 100000:
            setattr(worker, name, value)
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
        "session_limit": worker.session_limit,
        "message_limit": worker.message_limit,
        "dropped_events": worker.dropped_events,
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

WORKER_COMMAND_TEXT_LIMIT = 20000
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
        stored["text"] = text[:WORKER_COMMAND_TEXT_LIMIT]
        if payload.get("noReply") is not None:
            stored["noReply"] = bool(payload["noReply"])
        model = payload.get("model")
        if model is not None:
            stored["model"] = _validate_model(model)
    return stored


def _validate_model(model):
    if not isinstance(model, dict) or not model.get("providerID") or not model.get("modelID"):
        raise TaskError("model must carry providerID and modelID", 400)
    return {"providerID": str(model["providerID"])[:128], "modelID": str(model["modelID"])[:128]}


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
    actor_agent, actor_label, worker, action, payload, remote_session_id="", client_request_id=""
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
            if interaction.resolved_at and outcome["complete"]:
                # A complete snapshot re-observing a request re-opens it.
                interaction.resolved_at = None
                interaction.resolution = ""
            interaction.last_seen_at = now
            interaction.save()
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
    actor_agent, actor_label, task_id, worker, directory="", client_request_id=""
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
    command, _created = enqueue_worker_command(
        owner,
        actor_label or owner.name,
        worker,
        "create",
        {"directory": directory},
        remote_session_id="",
        client_request_id=create_request_id,
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
    task.log(
        TaskEvent.Type.EXECUTION_LAUNCHED,
        owner,
        audit_message(
            f"Execution attempt {attempt.pk} on {worker.name} ({directory}); command {command.pk}",
            actor_label if actor_agent is None else None,
        ),
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
