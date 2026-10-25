from django.conf import settings
from django.db.models import Count
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from . import services
from . import sync as sync_module
from .auth import get_agent, staff_name
from .models import (
    Agent,
    Project,
    Task,
    TaskExecutionAttempt,
    Worker,
    WorkerCommand,
    WorkerSession,
)
from .pagination import TaskPagination
from .queries import filter_tasks, order_tasks, task_queryset
from .serializers import (
    AgentRegistrationSerializer,
    ClaimNextSerializer,
    NoteSerializer,
    ReleaseSerializer,
    StatusUpdateSerializer,
    TaskDetailSerializer,
    TaskEventSerializer,
    TaskSerializer,
)


class AgentRegistrationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Staff configuration surface; agent keys never grant provisioning rights."""

    permission_classes = [IsAdminUser]
    http_method_names = ["get", "post", "head", "options"]
    serializer_class = AgentRegistrationSerializer
    queryset = Agent.objects.order_by("name", "pk")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        return response

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(
            [services.agent_registration_card(agent) for agent in page]
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent, key = services.register_agent(serializer.validated_data["name"])
        return Response(
            {**services.agent_registration_card(agent), "api_key": key},
            status=status.HTTP_201_CREATED,
        )


class WorkerViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Authenticated, credential-free worker registry reads (#10, #15).

    The registry stays readable by every agent; session data is scoped to the
    bound agent (#15): worker-scoped identity extends the project/task rules.
    """

    http_method_names = ["get", "head", "options"]
    lookup_field = "name"
    lookup_value_regex = r"[A-Za-z0-9_.-]{1,100}"

    def get_queryset(self):
        return Worker.objects.select_related("agent").order_by("name")

    def _session_worker(self):
        """Registry-wide lookup, then session-data scoping for agent keys."""
        worker = self.get_object()
        agent = get_agent(self.request)
        if agent and (worker.agent_id is None or worker.agent_id != agent.pk):
            raise PermissionDenied("Agents read only sessions of their bound worker")
        return worker

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(list(self.get_queryset()))
        return self.get_paginated_response([services.worker_read_model(w) for w in page])

    def retrieve(self, request, *args, **kwargs):
        worker = self.get_object()
        return Response(services.worker_read_model(worker, counts=True))

    @action(detail=True, methods=["get"], url_path="sessions")
    def sessions(self, request, name=None):
        worker = self._session_worker()
        sessions = services.filter_worker_sessions(
            worker.sessions.select_related("parent").order_by("-last_seen_at", "-pk"),
            directory=request.query_params.get("directory") or "",
            status=request.query_params.get("status") or "",
            project_id=request.query_params.get("project") or None,
        )
        page = self.paginate_queryset(sessions)
        return self.get_paginated_response([services.session_card(s) for s in page])

    @action(detail=True, methods=["get"], url_path=r"sessions/(?P<sid>[^/]{1,128})")
    def session_detail(self, request, name=None, sid=None):
        session = self._session_worker().sessions.filter(remote_id=sid).first()
        if session is None:
            raise services.TaskError("Unknown session for this worker", 404)
        return Response(
            services.session_detail(session, compact=request.query_params.get("compact") == "true")
        )

    @action(
        detail=True,
        methods=["get"],
        url_path=r"sessions/(?P<sid>[^/]{1,128})/messages",
    )
    def session_messages(self, request, name=None, sid=None):
        worker = self._session_worker()
        session = worker.sessions.filter(remote_id=sid).first()
        if session is None:
            raise services.TaskError("Unknown session for this worker", 404)
        after = request.query_params.get("after")
        before = request.query_params.get("before")
        if before is not None or request.query_params.get("latest") == "true":
            if after is not None:
                raise services.TaskError("Use after or latest/before, not both", 400)
            return Response(
                services.recent_messages_read(
                    session, before=before, limit=request.query_params.get("limit", 10)
                )
            )
        return Response(
            services.messages_read(
                session,
                after=after,
                limit=request.query_params.get("limit"),
            )
        )

    @action(detail=True, methods=["get"], url_path="events")
    def events(self, request, name=None):
        return Response(
            services.events_read(
                self._session_worker(),
                after=request.query_params.get("after"),
                limit=request.query_params.get("limit"),
            )
        )


class CommandSerializer(serializers.Serializer):
    worker = serializers.RegexField(r"[A-Za-z0-9_.-]{1,100}")
    action = serializers.ChoiceField(choices=[c for c, _ in WorkerCommand.ACTION_CHOICES])
    payload = serializers.DictField(child=serializers.JSONField(), required=False, default=dict)
    sessionID = serializers.RegexField(r"^.{1,128}$", required=False, allow_blank=False)
    client_request_id = serializers.RegexField(
        r"^.{1,64}$", required=False, allow_blank=True, default=""
    )


class ResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["done", "error"])
    result = serializers.JSONField(required=False)
    error = serializers.CharField(required=False, allow_blank=True)


class LaunchSerializer(serializers.Serializer):
    worker = serializers.RegexField(r"[A-Za-z0-9_.-]{1,100}")
    directory = serializers.CharField(required=False, allow_blank=True, max_length=500)
    client_request_id = serializers.RegexField(
        r"^.{1,64}$", required=False, allow_blank=True, default=""
    )


class AttemptLinkSerializer(serializers.Serializer):
    sessionID = serializers.RegexField(r"^.{1,128}$", allow_blank=False)


class CommandViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Durable command queue APIs (issue #12): staff any worker; agents own worker."""

    http_method_names = ["get", "post", "head", "options"]
    lookup_value_regex = r"\d+"

    def _agent(self):
        return get_agent(self.request)

    def get_queryset(self):
        query = WorkerCommand.objects.select_related("worker", "created_by")
        agent = self._agent()
        if agent:
            return query.filter(worker__agent=agent)
        return query

    def _scope_worker(self, name):
        agent = self._agent()
        worker = Worker.objects.filter(name=name).first()
        if worker is None:
            raise services.TaskError("Unknown worker", 404)
        if agent and worker.agent_id != agent.pk:
            raise PermissionDenied("Agents command only their bound worker")
        return worker

    def get_permissions(self):
        base = super().get_permissions()
        if self.action == "resolve":
            return base + [IsAdminUser()]
        return base

    def create(self, request, *args, **kwargs):
        agent = self._agent()
        staff_label = staff_name(request)
        if not agent and not staff_label:
            raise PermissionDenied("Authenticate as staff or a worker-bound agent")
        serializer = CommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        worker = self._scope_worker(data.pop("worker"))
        session_id = data.pop("sessionID", "") or ""
        command, created = services.enqueue_worker_command(
            agent,
            staff_label,
            worker,
            data["action"],
            data.get("payload") or {},
            remote_session_id=session_id,
            client_request_id=data.get("client_request_id") or "",
        )
        return Response(
            services.worker_command_card(command, detail=True),
            status=status.HTTP_202_ACCEPTED,
        )

    def list(self, request, *args, **kwargs):
        query = self.get_queryset()
        for name in ["status", "action"]:
            value = request.query_params.get(name)
            if value:
                query = query.filter(status=value)
        if request.query_params.get("worker"):
            query = query.filter(worker__name=request.query_params["worker"])
        page = self.paginate_queryset(query)
        return self.get_paginated_response(
            [services.worker_command_card(command, detail=True) for command in page]
        )

    def retrieve(self, request, *args, **kwargs):
        command = self.get_object()
        return Response(services.worker_command_card(command, detail=True))

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Explicit staff reconciliation for uncertain or pending commands (#12)."""
        command = self.get_object()
        serializer = ResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resolved = services.resolve_worker_command(
            command,
            serializer.validated_data["status"],
            serializer.validated_data.get("result"),
            serializer.validated_data.get("error") or "",
        )
        return Response(services.worker_command_card(resolved, detail=True))


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

    @action(detail=True, methods=["post"])
    def launch(self, request, pk=None):
        """Authorized remote launch (issue #13): atomic-claim-based, idempotent."""
        agent = get_agent(request)
        staff_label = staff_name(request)
        if not agent and not staff_label:
            raise PermissionDenied("Authenticate as staff or the task-owning agent")
        serializer = LaunchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        worker = Worker.objects.filter(name=data["worker"]).first()
        attempt, command, created = services.launch_task_execution(
            agent,
            staff_label,
            pk,
            worker,
            directory=data.get("directory") or "",
            client_request_id=data.get("client_request_id") or "",
        )
        return Response(
            {
                "attempt": services.attempt_read_model(attempt),
                "createCommand": services.worker_command_card(command, detail=True),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_202_ACCEPTED,
        )

    def retrieve(self, request, *args, **kwargs):
        detail = TaskDetailSerializer(
            fresh_task(self.get_object().pk), context=self.get_serializer_context()
        ).data
        # Attempts evidence rides the task detail (issue #13).
        detail["attempts"] = [
            services.attempt_read_model(attempt)
            for attempt in self.get_object()
            .execution_attempts.select_related("task", "worker", "owner")
            .order_by("id")
        ]
        return Response(detail)


@api_view(["POST"])
def attempt_link(request, pk):
    """Explicit authorized attempt↔session link after ownership checks (#13)."""
    agent = get_agent(request)
    staff_label = staff_name(request)
    if not agent and not staff_label:
        raise PermissionDenied("Authenticate as staff or the attempt owner")
    attempt = TaskExecutionAttempt.objects.select_related("task", "worker").filter(pk=pk).first()
    if attempt is None:
        raise services.TaskError("Unknown attempt", 404)
    if agent and attempt.owner_id != agent.pk and attempt.task.claimed_by_id != agent.pk:
        raise PermissionDenied("Only the attempt owner or staff links sessions")
    serializer = AttemptLinkSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    remote = serializer.validated_data["sessionID"]
    session = WorkerSession.objects.filter(worker=attempt.worker, remote_id=remote).first()
    linked = services.link_attempt_session(attempt, session)
    return Response(services.attempt_read_model(linked))


@api_view(["POST"])
def worker_sync(request):
    """Outbound bridge sync (#11): one authenticated envelope per cycle."""
    if not get_agent(request):
        raise PermissionDenied("Worker sync requires a worker-bound agent key")
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        content_length = 0
    if content_length > settings.WORKER_SYNC_MAX_BYTES:
        raise services.TaskError(
            f"Sync payload exceeds the configured limit of {settings.WORKER_SYNC_MAX_BYTES} bytes",
            413,
        )
    if not isinstance(request.data, dict):
        raise services.TaskError("Sync payload must be a JSON object", 400)
    intent = sync_module.parse_sync_payload(request.data)
    worker = services.verify_worker_write(intent["worker"], get_agent(request))
    return Response(services.apply_worker_sync(worker, intent))


@api_view(["GET"])
def sessions_read(request):
    """Unified cross-worker session list (#15).

    Workers stay isolated: the same remote ID on two workers remains two rows.
    Agents read only sessions of their bound worker; staff reads everything.
    """
    agent = get_agent(request)
    sessions = WorkerSession.objects.select_related("parent", "worker")
    if agent:
        sessions = sessions.filter(worker__agent=agent)
    ordering = (
        ("-remote_updated_ms", "-pk")
        if request.query_params.get("ordering") == "recent"
        else ("worker_id", "-last_seen_at", "-pk")
    )
    sessions = services.filter_worker_sessions(
        sessions.order_by(*ordering),
        directory=request.query_params.get("directory") or "",
        status=request.query_params.get("status") or "",
        project_id=request.query_params.get("project") or None,
    )
    paginator = TaskPagination()
    page = paginator.paginate_queryset(sessions, request, view=sessions_read)
    return paginator.get_paginated_response([services.session_card(s) for s in page])


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
