from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from .models import Project, Task, TaskEvent


class AgentRegistrationSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, trim_whitespace=True)


DEPENDENCY_PROGRESS = inline_serializer(
    "DependencyProgress",
    {"done": serializers.IntegerField(), "total": serializers.IntegerField()},
)


class TaskEventSerializer(serializers.ModelSerializer):
    agent = serializers.StringRelatedField()

    class Meta:
        model = TaskEvent
        fields = ["id", "event_type", "agent", "message", "created_at"]


class TaskSerializer(serializers.ModelSerializer):
    project = serializers.SlugRelatedField(slug_field="slug", queryset=Project.objects.all())
    depends_on = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        queryset=Task.objects.all(),
    )
    description = serializers.CharField(required=False, allow_blank=True, max_length=50000)
    priority = serializers.IntegerField(required=False, min_value=-2147483648, max_value=2147483647)
    claimed_by = serializers.StringRelatedField(read_only=True, allow_null=True)
    created_by = serializers.StringRelatedField(read_only=True, allow_null=True)
    is_ready = serializers.SerializerMethodField()
    dependency_progress = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "description",
            "project",
            "status",
            "priority",
            "claimed_by",
            "claimed_at",
            "heartbeat_at",
            "depends_on",
            "blocked_reason",
            "result_summary",
            "is_ready",
            "dependency_progress",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "status",
            "claimed_at",
            "heartbeat_at",
            "blocked_reason",
            "result_summary",
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_ready(self, obj):
        return (
            obj.status == Task.Status.PENDING
            and obj.claimed_by_id is None
            and all(dependency.status == Task.Status.DONE for dependency in obj.depends_on.all())
        )

    def validate_depends_on(self, value):
        if len(value) > 500:
            raise serializers.ValidationError("At most 500 dependencies are allowed")
        return value

    @extend_schema_field(DEPENDENCY_PROGRESS)
    def get_dependency_progress(self, obj):
        dependencies = list(obj.depends_on.all())
        return {
            "done": sum(task.status == Task.Status.DONE for task in dependencies),
            "total": len(dependencies),
        }


class TaskDetailSerializer(TaskSerializer):
    recent_events = serializers.SerializerMethodField()
    dependencies = serializers.SerializerMethodField()

    class Meta(TaskSerializer.Meta):
        fields = TaskSerializer.Meta.fields + ["recent_events", "dependencies"]

    @extend_schema_field(TaskEventSerializer(many=True))
    def get_recent_events(self, obj):
        return TaskEventSerializer(obj.events.select_related("agent")[:20], many=True).data

    @extend_schema_field(TaskSerializer(many=True))
    def get_dependencies(self, obj):
        return TaskSerializer(obj.depends_on.all(), many=True).data


class ClaimNextSerializer(serializers.Serializer):
    project = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Project.objects.all(),
        required=False,
        allow_null=True,
    )


class StatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=["in_progress", "blocked", "review", "done", "cancelled"]
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=20000)
    result_summary = serializers.CharField(required=False, allow_blank=True, max_length=50000)


class ReleaseSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=20000)


class NoteSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=20000)
