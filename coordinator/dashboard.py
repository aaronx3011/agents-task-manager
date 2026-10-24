from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework.exceptions import ValidationError

from .models import Agent, Project, Task, TaskEvent
from .queries import filter_tasks, task_queryset
from .serializers import TaskSerializer


@never_cache
@staff_member_required
@ensure_csrf_cookie
def dashboard(request):
    return render(request, "coordinator/dashboard.html", {"demo": settings.COORDINATOR_DEMO})


@never_cache
@staff_member_required
def dashboard_state(request):
    selected = request.GET.get("project", "")
    scoped_tasks = task_queryset()
    scoped_events = TaskEvent.objects.select_related("task__project", "agent")
    if selected:
        project = get_object_or_404(Project, slug=selected)
        scoped_tasks = scoped_tasks.filter(project=project)
        scoped_events = scoped_events.filter(task__project=project)
    active = scoped_tasks.exclude(status__in=["done", "cancelled"])
    finished = scoped_tasks.filter(status__in=["done", "cancelled"]).order_by("-updated_at")
    tasks = list(active[:200]) + list(finished[:40])
    counts = dict.fromkeys(Task.Status.values, 0)
    projects = list(Project.objects.order_by("name").values("slug", "name", "kind", "docs_path"))
    project_counts = {project["slug"]: dict.fromkeys(Task.Status.values, 0) for project in projects}
    for row in Task.objects.order_by().values("project__slug", "status").annotate(n=Count("id")):
        counts[row["status"]] += row["n"]
        project_counts.setdefault(row["project__slug"], dict.fromkeys(Task.Status.values, 0))[
            row["status"]
        ] = row["n"]
    ready_counts = {
        row["project__slug"]: row["n"]
        for row in Task.objects.ready().order_by().values("project__slug").annotate(n=Count("id"))
    }
    for project in projects:
        project["counts"] = project_counts[project["slug"]]
        project["ready_count"] = ready_counts.get(project["slug"], 0)
    agents = list(Agent.objects.order_by("name").values("name", "is_active", "last_seen"))
    events = [
        {
            "id": event.pk,
            "task_id": event.task_id,
            "title": event.task.title,
            "project": event.task.project.slug,
            "agent": event.agent.name if event.agent_id else "System / human",
            "event_type": event.event_type,
            "message": event.message,
            "created_at": event.created_at,
        }
        for event in scoped_events[:50]
    ]
    return JsonResponse(
        {
            "server_time": timezone.now(),
            "counts": counts,
            "projects": projects,
            "agents": agents,
            "tasks": TaskSerializer(tasks, many=True).data,
            "events": events,
            "ready_count": Task.objects.ready().count(),
            "total_tasks": sum(counts.values()),
            "displayed_tasks": len(tasks),
            "scope_project": selected,
            "scope_total_tasks": scoped_tasks.count(),
        }
    )


@never_cache
@staff_member_required
def workspace_state(request):
    # Share filtering with the list/column API so totals never depend on a loaded page.
    try:
        tasks = filter_tasks(request.GET)
    except ValidationError as exc:
        return JsonResponse({"detail": exc.detail}, status=400)
    counts = dict.fromkeys(Task.Status.values, 0)
    counts.update(
        {
            row["status"]: row["n"]
            for row in tasks.order_by().values("status").annotate(n=Count("id"))
        }
    )
    projects = list(Project.objects.order_by("name").values("slug", "name", "kind", "docs_path"))
    project_counts = {
        row["project__slug"]: row["n"]
        for row in Task.objects.exclude(status__in=["done", "cancelled"])
        .order_by()
        .values("project__slug")
        .annotate(n=Count("id"))
    }
    for project in projects:
        project["active_count"] = project_counts.get(project["slug"], 0)
    assignments = {}
    for task in Task.objects.filter(claimed_by__isnull=False).select_related("claimed_by"):
        assignments.setdefault(task.claimed_by_id, []).append(
            {
                "id": task.pk,
                "title": task.title,
                "status": task.status,
                "heartbeat_at": task.heartbeat_at,
            }
        )
    agents = [
        {
            "name": agent.name,
            "is_active": agent.is_active,
            "last_seen": agent.last_seen,
            "assignments": assignments.get(agent.pk, []),
        }
        for agent in Agent.objects.order_by("name")
    ]
    events = (
        TaskEvent.objects.filter(task__in=tasks.order_by().values("pk"))
        .select_related(
            "task__project",
            "agent",
        )
        .order_by("-pk")[:30]
    )
    return JsonResponse(
        {
            "server_time": timezone.now(),
            "counts": counts,
            "total": sum(counts.values()),
            "ready_count": tasks.ready().count(),
            "projects": projects,
            "agents": agents,
            "events": [
                {
                    "id": event.pk,
                    "task_id": event.task_id,
                    "title": event.task.title,
                    "project": event.task.project.slug,
                    "agent": event.agent.name if event.agent_id else "System / human",
                    "event_type": event.event_type,
                    "message": event.message,
                    "created_at": event.created_at,
                }
                for event in events
            ],
        }
    )
