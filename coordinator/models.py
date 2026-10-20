import secrets
from datetime import timedelta

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

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="events")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=20, choices=Type.choices)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
