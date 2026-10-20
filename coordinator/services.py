import time
from functools import wraps

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from .models import Task, TaskEvent


class TaskError(Exception):
    def __init__(self, message, status=409):
        self.message = message
        self.status = status
        super().__init__(message)


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
