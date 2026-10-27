import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone


class Project(models.Model):
    class Kind(models.TextChoices):
        CODE = "code", "Code"
        DESIGN = "design", "Design"
        CONTENT = "content", "Content"
        OTHER = "other", "Other"

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CODE)
    repo_url = models.URLField(blank=True)
    docs_path = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Agent(models.Model):
    name = models.CharField(max_length=100, unique=True)
    api_key_hash = models.CharField(max_length=256, editable=False)
    key_prefix = models.CharField(max_length=8, db_index=True, editable=False)
    is_active = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def set_new_api_key(self):
        raw = secrets.token_urlsafe(32)
        self.key_prefix = raw[:8]
        self.api_key_hash = make_password(raw)
        return raw

    @classmethod
    def authenticate(cls, raw_key):
        if not 32 <= len(raw_key) <= 128:
            return None
        for agent in cls.objects.filter(key_prefix=raw_key[:8], is_active=True):
            if check_password(raw_key, agent.api_key_hash):
                agent.last_seen = timezone.now()
                cls.objects.filter(pk=agent.pk).update(last_seen=agent.last_seen)
                return agent
        return None


class TaskQuerySet(models.QuerySet):
    def ready(self):
        unfinished = Task.objects.filter(dependents__pk=OuterRef("pk")).exclude(
            status=Task.Status.DONE
        )
        return self.filter(~Exists(unfinished), status=Task.Status.PENDING, claimed_by__isnull=True)

    def stale(self, minutes=15):
        return self.filter(
            status=Task.Status.IN_PROGRESS,
            heartbeat_at__lt=timezone.now() - timedelta(minutes=minutes),
        )


class Task(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        REVIEW = "review", "Review"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="tasks")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    priority = models.IntegerField(default=100)
    claimed_by = models.ForeignKey(
        Agent, null=True, blank=True, on_delete=models.PROTECT, related_name="tasks"
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    depends_on = models.ManyToManyField(
        "self", symmetrical=False, blank=True, related_name="dependents"
    )
    blocked_reason = models.TextField(blank=True)
    result_summary = models.TextField(blank=True)
    created_by = models.ForeignKey(
        Agent, null=True, blank=True, on_delete=models.PROTECT, related_name="created_tasks"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TaskQuerySet.as_manager()

    class Meta:
        ordering = ["priority", "created_at", "pk"]
        indexes = [
            models.Index(fields=["project", "status"]),
            models.Index(fields=["status", "heartbeat_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="pending") | Q(claimed_by__isnull=True),
                name="pending_tasks_unclaimed",
            ),
            models.CheckConstraint(
                condition=~Q(status="in_progress") | Q(claimed_by__isnull=False),
                name="in_progress_tasks_claimed",
            ),
            models.CheckConstraint(
                condition=(
                    Q(claimed_by__isnull=True, claimed_at__isnull=True, heartbeat_at__isnull=True)
                    | Q(
                        claimed_by__isnull=False,
                        claimed_at__isnull=False,
                        heartbeat_at__isnull=False,
                    )
                ),
                name="claim_metadata_consistent",
            ),
        ]

    def __str__(self):
        return f"[{self.project.slug}] {self.title}"

    @property
    def is_ready(self):
        return (
            self.status == self.Status.PENDING
            and self.claimed_by_id is None
            and not self.depends_on.exclude(status=self.Status.DONE).exists()
        )

    def log(self, event_type, agent=None, message=""):
        return TaskEvent.objects.create(
            task=self, agent=agent, event_type=event_type, message=message
        )


class TaskEvent(models.Model):
    class Type(models.TextChoices):
        CREATED = "created", "Created"
        CLAIMED = "claimed", "Claimed"
        HEARTBEAT = "heartbeat", "Heartbeat"
        STATUS_CHANGED = "status_changed", "Status changed"
        RELEASED = "released", "Released"
        NOTE = "note", "Note"
        EXECUTION_LAUNCHED = "execution.launched", "Execution launched"
        EXECUTION_LINKED = "execution.linked", "Execution session linked"
        EXECUTION_DONE = "execution.done", "Execution outcome"

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="events")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=20, choices=Type.choices)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class Worker(models.Model):
    """Persistent OpenCode worker bridge identity (contract #1/#8).

    The Worker row is provisioned explicitly and bound to exactly one Agent whose
    Bearer key authorizes its writes. Observed fields (sync health, endpoint and
    model metadata) are updated only by authenticated worker syncs and never carry
    OpenCode or provider credentials.
    """

    name = models.CharField(max_length=100, unique=True)
    agent = models.OneToOneField(Agent, null=True, on_delete=models.PROTECT, related_name="worker")
    is_enabled = models.BooleanField(default=True)
    hostname = models.CharField(max_length=255, blank=True)
    # Credential-free declared endpoint; userinfo is stripped on ingest.
    opencode_url = models.URLField(max_length=500, blank=True)
    protocol_version = models.PositiveIntegerField(null=True, blank=True)
    opencode_version = models.CharField(max_length=64, blank=True)
    # Observed directory scope and creation default from the bridge.
    directory = models.CharField(max_length=500, blank=True)
    create_directory = models.CharField(max_length=500, blank=True)
    # Local OpenCode reachability, SSE stream state and last bridge-side error.
    connected = models.BooleanField(default=False)
    stream_connected = models.BooleanField(default=False)
    last_error = models.TextField(blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    # Connected model/agent catalog observed from the local OpenCode endpoint.
    default_model = models.JSONField(null=True, blank=True)
    model_catalog = models.JSONField(default=list, blank=True)
    agent_catalog = models.JSONField(default=list, blank=True)
    # Inventory window limits and discarded-event counters reported by the bridge.
    session_limit = models.PositiveIntegerField(default=50)
    message_limit = models.PositiveIntegerField(default=30)
    dropped_events = models.PositiveIntegerField(default=0)
    # Worker heartbeat: freshness of the last authenticated, acknowledged sync.
    last_sync_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Acknowledged event batch for idempotent sync retries (#11).
    last_batch_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def is_online(self, now=None):
        if self.last_sync_at is None:
            return False
        threshold = timedelta(seconds=settings.WORKER_OFFLINE_SECONDS)
        now = now or timezone.now()
        return now - self.last_sync_at <= threshold


class ServerState(models.Model):
    """Durable server state; the sync epoch decides bridge snapshot republishing.

    Unlike the POC's per-process in-memory epoch, the epoch is a durable database
    row so different ASGI processes return the same value and planned resyncs are
    explicit (services.regenerate_sync_epoch). A restored/empty database yields a
    fresh epoch, which bridges treat as republish-snapshots.
    """

    sync_epoch = models.UUIDField(default=uuid.uuid4, editable=False)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["id"], name="server_state_singleton")]

    def __str__(self):
        return f"epoch={self.sync_epoch}"


class WorkerCommand(models.Model):
    """One durable remote command queued for a worker (issue #12).

    Delivery lifecycle: queued -> dispatched -> done | error, exactly as #8.
    "dispatched" is terminal for dispatch attempts (delivery is never auto-
    retried because a create/prompt may already have executed). Bridge-side
    uncertain delivery (receipt lost after potentially executing) is reported
    as an error whose text marks uncertainty; the server then flags it
    delivery_uncertain and keeps it visible until staff resolve it explicitly
    (services.resolve_worker_command). Worker results are idempotent: repeated
    uploads of the same outcome change nothing.
    """

    ACTION_CHOICES = [
        ("create", "create"),
        ("prompt", "prompt"),
        ("interrupt", "interrupt"),
        ("permission.reply", "permission.reply"),
        ("question.reply", "question.reply"),
        ("question.reject", "question.reject"),
    ]

    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, related_name="commands")
    created_by = models.ForeignKey(
        Agent, on_delete=models.PROTECT, null=True, blank=True, related_name="issued_commands"
    )
    actor_label = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    remote_session_id = models.CharField(max_length=128, blank=True)
    session = models.ForeignKey(
        "WorkerSession", on_delete=models.SET_NULL, null=True, blank=True, related_name="commands"
    )
    request_id = models.CharField(max_length=128, blank=True)
    # Absent ids are stored as NULL so empty strings cannot collide under the
    # (created_by, client_request_id) uniqueness across repeated commands.
    client_request_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    payload_fingerprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[
            ("queued", "queued"),
            ("dispatched", "dispatched"),
            ("done", "done"),
            ("error", "error"),
        ],
        default="queued",
        db_index=True,
    )
    delivery_uncertain = models.BooleanField(default=False)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    uncertain_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["worker", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "client_request_id"], name="client_request_key"
            ),
        ]

    def __str__(self):
        return f"{self.worker.name}/{self.pk}:{self.action}"


class PendingInteraction(models.Model):
    """A worker-scoped permission approval or ordered question batch (#14).

    POC parity: request rows come from the bridge's authoritative pending
    snapshot inside the sync envelope. Resolution happens ONLY through a
    complete snapshot (another client/TUI answered) — command failures or a
    partial/incomplete batch never resolve central state. A dispatched reply
    command marks this row reply_pending (visible, single active reply per
    request enforced in services) but resolution is still snapshot-owned.
    """

    KIND_CHOICES = [("permission", "permission"), ("question", "question")]

    worker = models.ForeignKey(
        Worker, on_delete=models.CASCADE, related_name="pending_interactions"
    )
    remote_session_id = models.CharField(max_length=128, blank=True, db_index=True)
    session = models.ForeignKey(
        "WorkerSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_interactions",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    request_id = models.CharField(max_length=128)
    data = models.JSONField(default=dict)
    reply_command = models.ForeignKey(
        "WorkerCommand", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reply_command_id_remote = models.CharField(max_length=64, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolution = models.CharField(max_length=64, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["worker", "kind"])]
        constraints = [
            models.UniqueConstraint(
                fields=["worker", "kind", "request_id"], name="worker_kind_request"
            ),
        ]

    def __str__(self):
        return f"{self.worker.name}/{self.kind}:{self.request_id}"


class WorkerSession(models.Model):
    """A session as seen by one worker, keyed by (worker, remote_session_id).

    Two workers reporting the same remote ID stay isolated. OpenCode remains the
    canonical history owner: the server keeps only the published bounded window
    plus completeness metadata, so a message missing from a later window is not
    silently treated as deleted.
    """

    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="sessions")
    remote_id = models.CharField(max_length=128, db_index=True)
    directory = models.CharField(max_length=500, blank=True)
    title = models.CharField(max_length=500, blank=True)
    agent_name = models.CharField(max_length=100, blank=True)
    model = models.JSONField(null=True, blank=True)
    # Remote parent/subagent linkage; parent resolves to a same-worker session row.
    remote_parent_id = models.CharField(max_length=128, blank=True, db_index=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="subagents"
    )
    # Remote endpoint execution state for this session (idle/busy/...), tracked
    # separately from machine-wide connectivity (#10) and discovery (#9 reads).
    execution_status = models.CharField(max_length=32, blank=True)
    last_error = models.TextField(blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    remote_created_ms = models.BigIntegerField(null=True, blank=True)
    remote_updated_ms = models.BigIntegerField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Published-window completeness metadata for the latest message snapshot.
    window_count = models.PositiveIntegerField(default=0)
    window_oldest_id = models.CharField(max_length=128, blank=True)
    window_complete = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["worker", "remote_id"], name="worker_remote_session")
        ]
        indexes = [models.Index(fields=["worker", "-last_seen_at"])]

    def __str__(self):
        return f"{self.worker.name}/{self.remote_id}"


class WorkerMessage(models.Model):
    """Upserted message snapshot with structured parts, per worker session."""

    session = models.ForeignKey(WorkerSession, on_delete=models.PROTECT, related_name="messages")
    remote_id = models.CharField(max_length=128, db_index=True)
    role = models.CharField(max_length=16, blank=True)
    content = models.JSONField(default=dict, blank=True)
    parts = models.JSONField(default=list, blank=True)
    remote_time = models.BigIntegerField(null=True, blank=True)
    published_at = models.DateTimeField(auto_now_add=True)
    in_last_window = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-remote_time", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["session", "remote_id"], name="session_remote_message")
        ]
        indexes = [models.Index(fields=["session", "-remote_time", "-id"])]

    def __str__(self):
        return f"{self.session.remote_id}/{self.remote_id}"


class WorkerEvent(models.Model):
    """Durable bounded worker observation; the global PK is the stable cursor."""

    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="events")
    session = models.ForeignKey(
        WorkerSession, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    remote_session_id = models.CharField(max_length=128, blank=True, db_index=True)
    event_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    # Deduplication key for repeated/republished identical frames.
    body_hash = models.CharField(max_length=64, blank=True, db_index=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-pk"]
        indexes = [models.Index(fields=["worker", "-id"])]

    def __str__(self):
        return f"{self.worker.name}/{self.pk}:{self.event_type}"


class TaskExecutionAttempt(models.Model):
    """One durable ready-and-launchable execution attempt of a task (issue #13).

    Lifecycle rules (#1 contract): the owner agent claims the task through the
    existing atomic services; launch then queues a create command on the owner's
    bound worker and links worker + workspace directory + create/prompt commands
    + eventually the observed OpenCode session. Remote events never move Task
    status — review/done stay explicit with the existing permission rules — and
    a worker heartbeat never renews task ownership (independent clocks).
    Exactly ONE attempt stays outcome=pending per task (partial unique
    constraint): replacements require resolving the running one first (no
    silent dual execution). Outcomes: pending → accepted | error | uncertain.
    """

    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        ERROR = "error", "Error"
        UNCERTAIN = "uncertain", "Uncertain"

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="execution_attempts")
    owner = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name="execution_attempts")
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="execution_attempts")
    directory = models.CharField(max_length=500, blank=True)
    create_command = models.ForeignKey(
        WorkerCommand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attempts_created",
    )
    prompt_command = models.ForeignKey(
        WorkerCommand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attempts_prompted",
    )
    remote_session_id = models.CharField(max_length=128, blank=True, db_index=True)
    session = models.ForeignKey(
        WorkerSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="execution_attempts",
    )
    outcome = models.CharField(
        max_length=16, choices=Outcome.choices, default=Outcome.PENDING, db_index=True
    )
    outcome_detail = models.TextField(blank=True)
    launched_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["task", "outcome"])]
        constraints = [
            models.UniqueConstraint(
                fields=["task"],
                name="one_pending_attempt_per_task",
                condition=Q(outcome="pending"),
            ),
        ]

    def __str__(self):
        return f"attempt {self.pk} task={self.task_id} worker={self.worker.name}:{self.outcome}"


def planning_columns():
    return ["Ideas", "Backlog", "Ready", "In progress", "Completed"]


class ProjectPlan(models.Model):
    project = models.OneToOneField(Project, on_delete=models.PROTECT, related_name="plan")
    default_worker = models.ForeignKey(Worker, null=True, blank=True, on_delete=models.PROTECT)
    directory = models.CharField(max_length=500, blank=True)
    model = models.JSONField(null=True, blank=True)
    # OpenCode agent name; blank inherits the worker's default agent.
    agent = models.CharField(max_length=100, blank=True)
    context = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    templates = models.JSONField(default=list, blank=True)
    columns = models.JSONField(default=planning_columns)
    playing = models.BooleanField(default=False)
    pause_reason = models.TextField(blank=True)
    revision = models.PositiveIntegerField(default=0)


class PlanningCard(models.Model):
    plan = models.ForeignKey(ProjectPlan, on_delete=models.PROTECT, related_name="cards")
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    checklist = models.JSONField(default=list, blank=True)
    labels = models.JSONField(default=list, blank=True)
    column = models.CharField(max_length=100, default="Backlog")
    position = models.PositiveIntegerField(default=0)
    instructions = models.TextField(blank=True)
    # Per-task overrides; blank/None inherit the project defaults.
    agent = models.CharField(max_length=100, blank=True)
    model = models.JSONField(null=True, blank=True)
    task = models.OneToOneField(Task, null=True, blank=True, on_delete=models.PROTECT)
    archived = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "pk"]


class PlanningJob(models.Model):
    plan = models.ForeignKey(ProjectPlan, on_delete=models.PROTECT, related_name="jobs")
    card = models.ForeignKey(PlanningCard, on_delete=models.PROTECT, related_name="jobs")
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT)
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default="queued", db_index=True)
    # Frozen configuration/card context at admission; final prompt adds prior results.
    snapshot = models.JSONField(default=dict)
    prompt = models.TextField(blank=True)
    report_token_hash = models.CharField(max_length=64, blank=True, editable=False)
    attempt = models.OneToOneField(
        TaskExecutionAttempt, null=True, blank=True, on_delete=models.PROTECT
    )
    stop_command = models.ForeignKey(
        WorkerCommand,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="planning_stops",
    )
    detail = models.TextField(blank=True)
    actor = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan"], condition=Q(status="running"), name="one_running_planning_job"
            ),
            models.UniqueConstraint(
                fields=["card"],
                condition=Q(status__in=["queued", "running"]),
                name="one_live_job_per_card",
            ),
        ]
