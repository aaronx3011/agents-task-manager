"""OpenAPI contract helpers for the versioned API (#39).

Everything here only describes the API for drf-spectacular; nothing changes how a
request is handled. Payloads assembled by coordinator/services.py read models are
documented as free-form JSON objects until they get dedicated serializers.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, inline_serializer
from rest_framework import serializers

from .serializers import TaskDetailSerializer, TaskEventSerializer, TaskSerializer

API_V1_PREFIX = "/api/v1/"

# A JSON object produced by a services.py read model (worker, session, command...).
READ_MODEL = OpenApiTypes.OBJECT


def only_versioned_endpoints(endpoints, **kwargs):
    """Document each endpoint once, under /api/v1/.

    The same routes are also mounted at /api/ for existing MCP and bridge clients;
    that alias is intentionally left out of the published contract.
    """
    return [endpoint for endpoint in endpoints if endpoint[0].startswith(API_V1_PREFIX)]


class AgentKeyScheme(OpenApiAuthenticationExtension):
    target_class = "coordinator.auth.AgentKeyAuthentication"
    name = "agentKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "Agent API key: `Authorization: Bearer <key>`.",
        }


def query(name, description, type_=str, enum=None):
    return OpenApiParameter(name, type_, OpenApiParameter.QUERY, description=description, enum=enum)


PAGINATION_PARAMS = [
    query("limit", "Page size, 1-500 (default 100).", int),
    query("offset", "Number of items to skip.", int),
]

SESSION_FILTER_PARAMS = [
    query("directory", "Only sessions in this worker directory."),
    query("status", "Only sessions with this status."),
    query("project", "Only sessions linked to this project id."),
]


def paginated(name, item):
    """The {count, items} envelope produced by coordinator.pagination.TaskPagination."""
    return inline_serializer(
        name,
        {"count": serializers.IntegerField(), "items": serializers.ListField(child=item)},
    )


OK_RESPONSE = inline_serializer("Ok", {"ok": serializers.BooleanField()})
HEARTBEAT_RESPONSE = inline_serializer(
    "HeartbeatResult",
    {"ok": serializers.BooleanField(), "heartbeat_at": serializers.DateTimeField()},
)
DETAIL_RESPONSE = inline_serializer("Detail", {"detail": serializers.CharField()})
TASK_EVENT_PAGE = inline_serializer(
    "TaskEventPage",
    {
        "items": TaskEventSerializer(many=True),
        "next_before": serializers.IntegerField(allow_null=True),
    },
)
LAUNCH_RESPONSE = inline_serializer(
    "TaskLaunch",
    {"attempt": serializers.JSONField(), "createCommand": serializers.JSONField()},
)
PROJECT_CREATED = inline_serializer(
    "ProjectCreated",
    {
        "slug": serializers.SlugField(),
        "name": serializers.CharField(),
        "kind": serializers.CharField(),
        "repo_url": serializers.CharField(),
        "docs_path": serializers.CharField(),
        "active_count": serializers.IntegerField(),
    },
)
PROJECT_OVERVIEW = inline_serializer(
    "ProjectOverview",
    {
        "project": serializers.SlugField(),
        "name": serializers.CharField(),
        "kind": serializers.CharField(),
        "repo_url": serializers.CharField(),
        "docs_path": serializers.CharField(),
        "counts": serializers.DictField(child=serializers.IntegerField()),
        "in_progress": serializers.ListField(child=serializers.JSONField()),
        "blocked": serializers.ListField(child=serializers.JSONField()),
        "ready_count": serializers.IntegerField(),
        "waiting": TaskSerializer(many=True),
    },
    many=True,
)

TASK_LIST_PARAMS = PAGINATION_PARAMS + [
    query("project", "Project slug."),
    query(
        "status",
        "Task status.",
        enum=["pending", "in_progress", "blocked", "review", "done", "cancelled"],
    ),
    query("claimed_by", "Agent name, or __unclaimed__."),
    query("ready", "true for ready tasks, false for not-ready tasks.", enum=["true", "false"]),
    query("q", "Search title/description, or #id."),
    query(
        "ordering",
        "Sort field, optionally prefixed with '-'.",
        enum=[
            prefix + field
            for field in ["priority", "title", "project", "status", "claimed_by", "updated_at"]
            for prefix in ("", "-")
        ],
    ),
]


class TaskDetailResponse(TaskDetailSerializer):
    """Task detail as returned by retrieve, including execution attempts (#13)."""

    attempts = serializers.ListField(child=serializers.JSONField(), read_only=True)

    class Meta(TaskDetailSerializer.Meta):
        fields = TaskDetailSerializer.Meta.fields + ["attempts"]


def read_model_page(name):
    """A paginated list of services.py read models."""
    return paginated(name, serializers.JSONField())
