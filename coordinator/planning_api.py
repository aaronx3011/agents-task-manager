"""Staff planning HTTP boundary. Mutations live in services.py."""

from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from . import services
from .auth import get_agent, staff_name
from .models import Project, Worker


class ProjectInput(serializers.Serializer):
    slug = serializers.SlugField(max_length=50)
    name = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(choices=Project.Kind.choices, default=Project.Kind.CODE)
    repo_url = serializers.URLField(max_length=200, required=False, allow_blank=True, default="")
    docs_path = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


@api_view(["POST"])
@permission_classes([IsAdminUser])
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
    agent = serializers.CharField(max_length=100, allow_blank=True, required=False)
    model = ModelChoice(allow_null=True, required=False)


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

    def validate(self, data):
        field = {
            "settings": "settings",
            "card": "card",
            "enqueue": "card_id",
            "run": "card_id",
            "remove": "job_id",
            "archive": "card_id",
            "reorder": "job_ids",
        }.get(data["operation"])
        if field and field not in data:
            raise serializers.ValidationError({field: "This field is required"})
        return data


@api_view(["GET", "POST"])
@permission_classes([IsAdminUser])
def project_planning(request, slug):
    project = Project.objects.filter(slug=slug).first()
    if not project:
        raise services.TaskError("Project not found", 404)
    if request.method == "POST":
        serializer = PlanningMutation(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.mutate_planning(project, staff_name(request), serializer.validated_data)
    return Response(services.planning_read(project))


class JobReport(serializers.Serializer):
    token = serializers.CharField(min_length=32, max_length=100, trim_whitespace=False)
    status = serializers.ChoiceField(choices=["heartbeat", "done", "blocked", "review"])
    summary = serializers.CharField(max_length=10000, required=False, allow_blank=True, default="")


@api_view(["POST"])
def planning_report(request, pk):
    agent = get_agent(request)
    if not agent:
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied("An authenticated agent and job-scoped report token are required")
    serializer = JobReport(data=request.data)
    serializer.is_valid(raise_exception=True)
    return Response(services.report_planning_job(agent, pk, **serializer.validated_data))
