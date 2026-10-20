from django.db.models import Count
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from . import services
from .auth import get_agent, staff_name
from .models import Project, Task
from .queries import filter_tasks, order_tasks, task_queryset
from .serializers import (
    ClaimNextSerializer,
    NoteSerializer,
    ReleaseSerializer,
    StatusUpdateSerializer,
    TaskDetailSerializer,
    TaskEventSerializer,
    TaskSerializer,
)


@services.retry_sqlite
def fresh_task(task_id):
    # Re-serializes committed state after a mutation; on SQLite the read may briefly
    # collide with another writer's transaction, so it shares the bounded retry.
    return task_queryset().get(pk=task_id)


class TaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    http_method_names = ["get", "post", "patch", "head", "options"]
    lookup_value_regex = r"\d+"

    def get_serializer_class(self):
        return TaskDetailSerializer if self.action == "retrieve" else TaskSerializer

    def get_queryset(self):
        if self.action != "list":
            return task_queryset()
        return order_tasks(
            filter_tasks(self.request.query_params),
            self.request.query_params.get("ordering", "priority"),
        )

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        task = self.get_object()
        events = task.events.select_related("agent")
        before = request.query_params.get("before")
        if before is not None:
            if not before.isascii() or not before.isdigit() or len(before) > 18:
                raise ValidationError({"before": "Must be a positive event ID"})
            events = events.filter(pk__lt=int(before))
        # Audit IDs are stable cursors while newer events arrive concurrently.
        rows = list(events.order_by("-pk")[:21])
        return Response(
            {
                "items": TaskEventSerializer(rows[:20], many=True).data,
                "next_before": rows[19].pk if len(rows) > 20 else None,
            }
        )

    def task_response(self, task, response_status=status.HTTP_200_OK):
        return Response(TaskSerializer(fresh_task(task.pk)).data, status=response_status)

    def actor(self):
        return {"agent": get_agent(self.request), "human_actor": staff_name(self.request)}

    def require_agent(self):
        agent = get_agent(self.request)
        if not agent:
            raise PermissionDenied("Only agents can claim tasks.")
        return agent

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data["depends_on"] = [task.pk for task in data.get("depends_on", [])]
        task = services.create_task(**self.actor(), **data)
        return self.task_response(task, status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        task = services.edit_task(task_id=pk, changes=serializer.validated_data, **self.actor())
        return self.task_response(task)

    @action(detail=False, methods=["post"])
    def next(self, request):
        agent = self.require_agent()
        serializer = ClaimNextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = services.claim_next(agent, serializer.validated_data.get("project"))
        if task is None:
            return Response({"detail": "No ready tasks."}, status=status.HTTP_404_NOT_FOUND)
        return self.task_response(task)

    @action(detail=True, methods=["post"])
    def claim(self, request, pk=None):
        return self.task_response(services.claim_task(self.require_agent(), pk))

    @action(detail=True, methods=["post"])
    def heartbeat(self, request, pk=None):
        task = services.heartbeat(task_id=pk, **self.actor())
        return Response({"ok": True, "heartbeat_at": task.heartbeat_at})

    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request, pk=None):
        serializer = StatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = services.update_status(task_id=pk, **self.actor(), **serializer.validated_data)
        return self.task_response(task)

    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        serializer = ReleaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        task = services.release_task(task_id=pk, **self.actor(), **serializer.validated_data)
        return self.task_response(task)

    @action(detail=True, methods=["post"])
    def notes(self, request, pk=None):
        serializer = NoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_note(task_id=pk, **self.actor(), **serializer.validated_data)
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def overview(request):
    result = []
    for project in Project.objects.order_by("slug"):
        counts = dict.fromkeys(Task.Status.values, 0)
        counts.update(
            {
                row["status"]: row["total"]
                for row in project.tasks.values("status").annotate(total=Count("id"))
            }
        )
        active = list(task_queryset().filter(project=project, status="in_progress"))
        blocked = list(project.tasks.filter(status="blocked"))
        result.append(
            {
                "project": project.slug,
                "name": project.name,
                "kind": project.kind,
                "repo_url": project.repo_url,
                "docs_path": project.docs_path,
                "counts": counts,
                "in_progress": [
                    {
                        "id": task.pk,
                        "title": task.title,
                        "agent": str(task.claimed_by),
                        "heartbeat_at": task.heartbeat_at,
                    }
                    for task in active
                ],
                "blocked": [
                    {"id": t.pk, "title": t.title, "reason": t.blocked_reason} for t in blocked
                ],
                "ready_count": project.tasks.ready().count(),
                "waiting": TaskSerializer(
                    task_queryset()
                    .filter(
                        project=project,
                        status="pending",
                    )
                    .exclude(pk__in=Task.objects.ready().values("pk")),
                    many=True,
                ).data,
            }
        )
    return Response(result)
