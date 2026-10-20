from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from .models import Project, Task


def task_queryset():
    return Task.objects.select_related("project", "claimed_by", "created_by").prefetch_related(
        "depends_on__project",
        "depends_on__claimed_by",
        "depends_on__created_by",
        "depends_on__depends_on",
    )


def filter_tasks(params, queryset=None):
    tasks = task_queryset() if queryset is None else queryset
    if params.get("project"):
        tasks = tasks.filter(project=get_object_or_404(Project, slug=params["project"]))
    if params.get("status"):
        if params["status"] not in Task.Status.values:
            raise ValidationError({"status": "Invalid task status"})
        tasks = tasks.filter(status=params["status"])
    if params.get("claimed_by"):
        if params["claimed_by"] == "__unclaimed__":
            tasks = tasks.filter(claimed_by__isnull=True)
        else:
            tasks = tasks.filter(claimed_by__name=params["claimed_by"])
    if params.get("ready"):
        if params["ready"].lower() == "true":
            tasks = tasks.ready()
        elif params["ready"].lower() == "false":
            tasks = tasks.exclude(pk__in=Task.objects.ready().values("pk"))
        else:
            raise ValidationError({"ready": "Must be true or false"})
    search = params.get("q", "").strip()
    if len(search) > 300:
        raise ValidationError({"q": "Search must be at most 300 characters"})
    if search:
        match = Q(title__icontains=search) | Q(description__icontains=search)
        task_id = search.removeprefix("#")
        if task_id.isascii() and task_id.isdigit() and len(task_id) < 19:
            match |= Q(pk=int(task_id))
        tasks = tasks.filter(match)
    return tasks


def order_tasks(tasks, ordering="priority"):
    allowed = {
        "priority": "priority",
        "title": "title",
        "project": "project__name",
        "status": "status",
        "claimed_by": "claimed_by__name",
        "updated_at": "updated_at",
    }
    key = ordering.removeprefix("-")
    if key not in allowed:
        raise ValidationError({"ordering": "Unsupported sort field"})
    field = ("-" if ordering.startswith("-") else "") + allowed[key]
    return tasks.order_by(field, "created_at", "pk")
