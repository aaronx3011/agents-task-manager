"""Dashboard read models served by DRF under /api/v1/dashboard/ (#40).

The SPA reads these through /api/v1; the old /dashboard/*/ aliases were removed
with the SPA split (#50).
"""

from django.conf import settings
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth import IsViewer
from .models import Agent, Project, Task, TaskEvent
from .openapi import PAGINATION_PARAMS, TASK_LIST_PARAMS
from .queries import filter_tasks, task_queryset
from .serializers import TaskSerializer

FINISHED = ["done", "cancelled"]
STATE_ACTIVE_LIMIT = 200
STATE_FINISHED_LIMIT = 40
STATE_EVENT_LIMIT = 50
WORKSPACE_EVENT_LIMIT = 30


def empty_counts():
    return dict.fromkeys(Task.Status.values, 0)


StatusCountsSerializer = type(
    "StatusCountsSerializer",
    (serializers.Serializer,),
    {status: serializers.IntegerField() for status in Task.Status.values},
)


class DashboardEventSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="pk")
    task_id = serializers.IntegerField()
    title = serializers.CharField(source="task.title")
    project = serializers.CharField(source="task.project.slug")
    agent = serializers.SerializerMethodField()
    event_type = serializers.CharField()
    message = serializers.CharField()
    created_at = serializers.DateTimeField()

    def get_agent(self, event) -> str:
        return event.agent.name if event.agent_id else "System / human"


class DashboardProjectSerializer(serializers.Serializer):
    slug = serializers.CharField()
    name = serializers.CharField()
    kind = serializers.CharField()
    docs_path = serializers.CharField()


class StateProjectSerializer(DashboardProjectSerializer):
    counts = StatusCountsSerializer()
    ready_count = serializers.IntegerField()


class WorkspaceProjectSerializer(DashboardProjectSerializer):
    active_count = serializers.IntegerField()


class DashboardAgentSerializer(serializers.Serializer):
    name = serializers.CharField()
    is_active = serializers.BooleanField()
    last_seen = serializers.DateTimeField(allow_null=True)


class AssignmentSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="pk")
    title = serializers.CharField()
    status = serializers.ChoiceField(choices=Task.Status.choices)
    heartbeat_at = serializers.DateTimeField(allow_null=True)


class WorkspaceAgentSerializer(DashboardAgentSerializer):
    assignments = AssignmentSerializer(many=True)


FILTER_ERROR = inline_serializer(
    "WorkspaceFilterError",
    {"detail": serializers.DictField(child=serializers.CharField())},
)


class DashboardStateSerializer(serializers.Serializer):
    server_time = serializers.DateTimeField()
    counts = StatusCountsSerializer()
    projects = StateProjectSerializer(many=True)
    agents = DashboardAgentSerializer(many=True)
    tasks = TaskSerializer(many=True)
    events = DashboardEventSerializer(many=True)
    ready_count = serializers.IntegerField()
    total_tasks = serializers.IntegerField()
    displayed_tasks = serializers.IntegerField()
    scope_project = serializers.CharField()
    scope_total_tasks = serializers.IntegerField()


class WorkspaceStateSerializer(serializers.Serializer):
    server_time = serializers.DateTimeField()
    counts = StatusCountsSerializer()
    total = serializers.IntegerField()
    ready_count = serializers.IntegerField()
    projects = WorkspaceProjectSerializer(many=True)
    agents = WorkspaceAgentSerializer(many=True)
    events = DashboardEventSerializer(many=True)


def project_rows():
    return list(Project.objects.order_by("name").values("slug", "name", "kind", "docs_path"))


def dashboard_state(selected):
    scoped_tasks = task_queryset()
    scoped_events = TaskEvent.objects.select_related("task__project", "agent")
    if selected:
        project = get_object_or_404(Project, slug=selected)
        scoped_tasks = scoped_tasks.filter(project=project)
        scoped_events = scoped_events.filter(task__project=project)
    active = scoped_tasks.exclude(status__in=FINISHED)
    finished = scoped_tasks.filter(status__in=FINISHED).order_by("-updated_at")
    tasks = list(active[:STATE_ACTIVE_LIMIT]) + list(finished[:STATE_FINISHED_LIMIT])
    counts = empty_counts()
    projects = project_rows()
    project_counts = {project["slug"]: empty_counts() for project in projects}
    for row in Task.objects.order_by().values("project__slug", "status").annotate(n=Count("id")):
        counts[row["status"]] += row["n"]
        project_counts.setdefault(row["project__slug"], empty_counts())[row["status"]] = row["n"]
    ready_counts = {
        row["project__slug"]: row["n"]
        for row in Task.objects.ready().order_by().values("project__slug").annotate(n=Count("id"))
    }
    for project in projects:
        project["counts"] = project_counts[project["slug"]]
        project["ready_count"] = ready_counts.get(project["slug"], 0)
    return {
        "server_time": timezone.now(),
        "counts": counts,
        "projects": projects,
        "agents": Agent.objects.order_by("name"),
        "tasks": tasks,
        "events": scoped_events[:STATE_EVENT_LIMIT],
        "ready_count": Task.objects.ready().count(),
        "total_tasks": sum(counts.values()),
        "displayed_tasks": len(tasks),
        "scope_project": selected,
        "scope_total_tasks": scoped_tasks.count(),
    }


def workspace_state(params):
    # Share filtering with the list/column API so totals never depend on a loaded page.
    tasks = filter_tasks(params)
    counts = empty_counts()
    counts.update(
        {
            row["status"]: row["n"]
            for row in tasks.order_by().values("status").annotate(n=Count("id"))
        }
    )
    projects = project_rows()
    active_counts = {
        row["project__slug"]: row["n"]
        for row in Task.objects.exclude(status__in=FINISHED)
        .order_by()
        .values("project__slug")
        .annotate(n=Count("id"))
    }
    for project in projects:
        project["active_count"] = active_counts.get(project["slug"], 0)
    assignments = {}
    for task in Task.objects.filter(claimed_by__isnull=False).select_related("claimed_by"):
        assignments.setdefault(task.claimed_by_id, []).append(task)
    agents = list(Agent.objects.order_by("name"))
    for agent in agents:
        agent.assignments = assignments.get(agent.pk, [])
    events = (
        TaskEvent.objects.filter(task__in=tasks.order_by().values("pk"))
        .select_related("task__project", "agent")
        .order_by("-pk")[:WORKSPACE_EVENT_LIMIT]
    )
    return {
        "server_time": timezone.now(),
        "counts": counts,
        "total": sum(counts.values()),
        "ready_count": tasks.ready().count(),
        "projects": projects,
        "agents": agents,
        "events": events,
    }


@method_decorator(never_cache, name="dispatch")
class DashboardStateView(APIView):
    permission_classes = [IsViewer]

    @extend_schema(
        operation_id="dashboard_state",
        summary="Dashboard snapshot: counts, projects, agents, recent tasks and events",
        parameters=[
            OpenApiParameter("project", str, description="Scope tasks and events to a slug.")
        ],
        responses=DashboardStateSerializer,
    )
    def get(self, request):
        state = dashboard_state(request.query_params.get("project", ""))
        return Response(DashboardStateSerializer(state).data)


@method_decorator(never_cache, name="dispatch")
class WorkspaceStateView(APIView):
    permission_classes = [IsViewer]

    @extend_schema(
        operation_id="dashboard_workspace",
        summary="Workspace totals, agent assignments and events for a task filter",
        parameters=TASK_LIST_PARAMS[len(PAGINATION_PARAMS) :],
        responses={200: WorkspaceStateSerializer, 400: FILTER_ERROR},
    )
    def get(self, request):
        try:
            state = workspace_state(request.query_params)
        except ValidationError as exc:
            # Legacy payload shape kept so /dashboard/workspace/ clients stay unchanged.
            return Response({"detail": exc.detail}, status=400)
        return Response(WorkspaceStateSerializer(state).data)


@method_decorator(never_cache, name="dispatch")
class MetaView(APIView):
    """Public, data-free facts the SPA needs before sign-in (#50)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id="meta",
        summary="Deployment facts for the SPA (no authentication)",
        responses=inline_serializer(
            "Meta",
            {
                "demo": serializers.BooleanField(),
                "admin_url": serializers.CharField(allow_null=True),
            },
        ),
        auth=[],
    )
    def get(self, request):
        return Response(
            {
                "demo": bool(settings.COORDINATOR_DEMO),
                # Where superusers find Django admin; null when it is disabled.
                "admin_url": f"/{settings.ADMIN_PATH}" if settings.ADMIN_ENABLED else None,
            }
        )
