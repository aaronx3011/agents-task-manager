"""Planning HTTP boundary (viewers read, members write). Mutations live in services.py."""

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from . import services
from .auth import (
    AgentOrViewerReads,
    IsMember,
    ViewerReadsMemberWrites,
    get_agent,
    human_actor,
)
from .models import Project, Task, Worker
from .openapi import PROJECT_CREATED, READ_MODEL


class ProjectInput(serializers.Serializer):
    slug = serializers.SlugField(max_length=50)
    name = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(choices=Project.Kind.choices, default=Project.Kind.CODE)
    repo_url = serializers.URLField(max_length=200, required=False, allow_blank=True, default="")
    docs_path = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


@extend_schema(request=ProjectInput, responses={201: PROJECT_CREATED})
@api_view(["POST"])
@permission_classes([IsMember])
def projects_create(request):
    serializer = ProjectInput(data=request.data)
    serializer.is_valid(raise_exception=True)
    project = services.create_project(**serializer.validated_data)
    return Response(
        {
            "slug": project.slug,
            "name": project.name,
            "kind": project.kind,
            "repo_url": project.repo_url,
            "docs_path": project.docs_path,
            "active_count": 0,
        },
        status=201,
    )


class ModelChoice(serializers.Serializer):
    providerID = serializers.CharField(max_length=100)
    modelID = serializers.CharField(max_length=200)


class PromptTemplate(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    text = serializers.CharField(max_length=8000)


class PlanSettings(serializers.Serializer):
    worker = serializers.CharField(max_length=100, allow_blank=True)
    directory = serializers.CharField(max_length=500, allow_blank=True)
    model = ModelChoice(allow_null=True)
    agent = serializers.CharField(max_length=100, allow_blank=True, default="")
    context = serializers.CharField(max_length=8000, allow_blank=True)
    instructions = serializers.CharField(max_length=8000, allow_blank=True)
    # Hand-off default for cards launched from this project (#137).
    handoff_mode = serializers.ChoiceField(
        choices=Task.HandoffMode.choices, required=False, default=Task.HandoffMode.OFF
    )
    templates = PromptTemplate(many=True, max_length=30)
    columns = serializers.ListField(
        child=serializers.CharField(max_length=100), min_length=1, max_length=20
    )

    def validate_worker(self, value):
        if value and not Worker.objects.filter(name=value).exists():
            raise serializers.ValidationError("Unknown worker")
        return value

    def validate_columns(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("Column names must be unique")
        return value

    def validate_templates(self, value):
        if len({item["name"] for item in value}) != len(value):
            raise serializers.ValidationError("Prompt names must be unique")
        return value


class ChecklistItem(serializers.Serializer):
    text = serializers.CharField(max_length=500)
    done = serializers.BooleanField(default=False)


class CardInput(serializers.Serializer):
    id = serializers.IntegerField(min_value=1, required=False)
    title = serializers.CharField(max_length=300)
    description = serializers.CharField(max_length=12000, allow_blank=True)
    column = serializers.CharField(max_length=100)
    position = serializers.IntegerField(min_value=0, required=False)
    labels = serializers.ListField(child=serializers.CharField(max_length=50), max_length=20)
    checklist = ChecklistItem(many=True, max_length=100)
    instructions = serializers.CharField(max_length=8000, allow_blank=True)
    # Blank runs the card on the project's default worker.
    worker = serializers.CharField(max_length=100, allow_blank=True, required=False)
    agent = serializers.CharField(max_length=100, allow_blank=True, required=False)
    model = ModelChoice(allow_null=True, required=False)
    # Blank inherits the project's hand-off default (#137).
    handoff_mode = serializers.ChoiceField(
        choices=Task.HandoffMode.choices, required=False, allow_blank=True
    )

    def validate_worker(self, value):
        if value and not Worker.objects.filter(name=value).exists():
            raise serializers.ValidationError("Unknown worker")
        return value


class PipeStageInput(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(choices=services.PipeStage.Role.choices, default="custom")
    instructions = serializers.CharField(
        max_length=8000, allow_blank=True, required=False, default=""
    )
    # Blank uses the project default worker; blank model/agent use its defaults.
    worker = serializers.CharField(max_length=100, allow_blank=True, required=False, default="")
    agent = serializers.CharField(max_length=100, allow_blank=True, required=False, default="")
    model = ModelChoice(allow_null=True, required=False, default=None)
    directory = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")
    requires_approval = serializers.BooleanField(default=False)
    card_id = serializers.IntegerField(min_value=1, required=False)

    def validate_worker(self, value):
        if value and not Worker.objects.filter(name=value).exists():
            raise serializers.ValidationError("Unknown worker")
        return value


class PipeInput(serializers.Serializer):
    title = serializers.CharField(max_length=300)
    description = serializers.CharField(
        max_length=12000, allow_blank=True, required=False, default=""
    )
    on_failure = serializers.ChoiceField(choices=services.Pipe.Failure.choices, default="stop")
    max_retries = serializers.IntegerField(min_value=0, max_value=5, default=1)
    play = serializers.BooleanField(default=False)
    stages = PipeStageInput(many=True, min_length=1, max_length=10)


class PlanningMutation(serializers.Serializer):
    revision = serializers.IntegerField(min_value=0)
    operation = serializers.ChoiceField(
        choices=[
            "settings",
            "card",
            "enqueue",
            "run",
            "play",
            "pause",
            "stop",
            "reorder",
            "remove",
            "archive",
            "pipe_create",
            "pipe_approve",
            "pipe_stop",
        ]
    )
    settings = PlanSettings(required=False)
    card = CardInput(required=False)
    card_id = serializers.IntegerField(min_value=1, required=False)
    job_id = serializers.IntegerField(min_value=1, required=False)
    job_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), max_length=100, required=False
    )
    instructions = serializers.CharField(max_length=8000, allow_blank=True, required=False)
    template = serializers.CharField(max_length=8000, allow_blank=True, required=False)
    pipe = PipeInput(required=False)
    pipe_id = serializers.IntegerField(min_value=1, required=False)

    def validate(self, data):
        field = {
            "settings": "settings",
            "card": "card",
            "enqueue": "card_id",
            "run": "card_id",
            "remove": "job_id",
            "archive": "card_id",
            "reorder": "job_ids",
            "pipe_create": "pipe",
            "pipe_approve": "pipe_id",
            "pipe_stop": "pipe_id",
        }.get(data["operation"])
        if field and field not in data:
            raise serializers.ValidationError({field: "This field is required"})
        return data


@extend_schema(methods=["GET"], responses=READ_MODEL)
@extend_schema(methods=["POST"], request=PlanningMutation, responses=READ_MODEL)
@api_view(["GET", "POST"])
@permission_classes([ViewerReadsMemberWrites])
def project_planning(request, slug):
    project = Project.objects.filter(slug=slug).first()
    if not project:
        raise services.TaskError("Project not found", 404)
    if request.method == "POST":
        serializer = PlanningMutation(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.mutate_planning(project, human_actor(request), serializer.validated_data)
    return Response(services.planning_read(project))


def _project_or_404(slug):
    project = Project.objects.filter(slug=slug).first()
    if not project:
        raise services.TaskError("Project not found", 404)
    return project


@extend_schema(
    operation_id="project_pipes_list",
    responses=READ_MODEL,
    parameters=[
        OpenApiParameter(
            "limit", int, default=20, description="Pipes to return, 1–100 (newest first)."
        ),
        OpenApiParameter("offset", int, default=0, description="Pipes to skip."),
    ],
)
@api_view(["GET"])
@permission_classes([AgentOrViewerReads])
def project_pipes(request, slug):
    """Read a project's pipes and stages; agent keys and viewers may read."""
    project = _project_or_404(slug)
    try:
        limit = int(request.query_params.get("limit", 20))
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError):
        raise services.TaskError("limit and offset must be integers", 400) from None
    if not 1 <= limit <= 100:
        raise services.TaskError("limit must be between 1 and 100", 400)
    if offset < 0:
        raise services.TaskError("offset must be zero or greater", 400)
    return Response(services.pipes_read(project, limit=limit, offset=offset))


@extend_schema(operation_id="project_pipe_retrieve", responses=READ_MODEL)
@api_view(["GET"])
@permission_classes([AgentOrViewerReads])
def project_pipe(request, slug, pipe_id):
    """Read one pipe owned by the project; a foreign pipe is a 404."""
    project = _project_or_404(slug)
    return Response(services.pipe_read(project, pipe_id))


class JobReport(serializers.Serializer):
    token = serializers.CharField(min_length=32, max_length=100, trim_whitespace=False)
    status = serializers.ChoiceField(choices=["heartbeat", "done", "blocked", "review"])
    summary = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")


@extend_schema(request=JobReport, responses=READ_MODEL)
@api_view(["POST"])
def planning_report(request, pk):
    agent = get_agent(request)
    if not agent:
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("An authenticated agent and job-scoped report token are required")
    serializer = JobReport(data=request.data)
    serializer.is_valid(raise_exception=True)
    return Response(services.report_planning_job(agent, pk, **serializer.validated_data))
