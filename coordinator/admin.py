from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponse

from . import services
from .models import Agent, Project, Task, TaskEvent, Worker, WorkerCommand


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("name", "agent", "is_enabled", "online", "connected", "last_sync_at")
    list_filter = ("is_enabled", "connected")
    search_fields = ("name", "hostname", "directory")
    readonly_fields = (
        "hostname",
        "opencode_url",
        "protocol_version",
        "opencode_version",
        "directory",
        "create_directory",
        "connected",
        "stream_connected",
        "last_error",
        "last_error_at",
        "default_model",
        "model_catalog",
        "session_limit",
        "message_limit",
        "dropped_events",
        "last_sync_at",
        "created_at",
        "updated_at",
    )
    actions = ["disable_selected", "enable_selected"]

    @admin.display(boolean=True)
    def online(self, obj):
        return obj.is_online()

    @admin.action(description="Disable selected workers (blocks ingestion/delivery)")
    def disable_selected(self, request, queryset):
        for worker in queryset:
            services.set_worker_enabled(worker, enabled=False, reason="disabled by admin")
        self.message_user(request, f"Disabled {queryset.count()} worker(s).")

    @admin.action(description="Enable selected workers")
    def enable_selected(self, request, queryset):
        for worker in queryset:
            services.set_worker_enabled(worker, enabled=True, reason="enabled by admin")
        self.message_user(request, f"Enabled {queryset.count()} worker(s).")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "kind", "docs_path")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "last_seen")
    readonly_fields = ("last_seen", "created_at")
    actions = ["regenerate_key"]

    def save_model(self, request, obj, form, change):
        if not change:
            raw = obj.set_new_api_key()
            messages.warning(request, f"API key for {obj.name} (shown once): {raw}")
        super().save_model(request, obj, form, change)

    @admin.action(description="Regenerate API key (invalidates old key)")
    def regenerate_key(self, request, queryset):
        for agent in queryset:
            raw = agent.set_new_api_key()
            agent.save(update_fields=["api_key_hash", "key_prefix"])
            messages.warning(request, f"{agent.name} (shown once): {raw}")


class TaskAdminForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = "__all__"

    def clean(self):
        data = super().clean()
        dependencies = data.get("depends_on", [])
        target = self.instance.pk
        visited = set()
        stack = [dependency.pk for dependency in dependencies]
        while stack:
            pk = stack.pop()
            if pk == target:
                raise forms.ValidationError("Dependencies must not contain a cycle.")
            if pk not in visited:
                visited.add(pk)
                stack.extend(Task.objects.get(pk=pk).depends_on.values_list("pk", flat=True))
        status = data.get("status")
        owner = data.get("claimed_by")
        if status == Task.Status.IN_PROGRESS and not owner:
            self.add_error("claimed_by", "In-progress tasks require an owner.")
        if status == Task.Status.PENDING and owner:
            self.add_error("claimed_by", "Pending tasks must be unclaimed.")
        if status == Task.Status.BLOCKED and not data.get("blocked_reason", "").strip():
            self.add_error("blocked_reason", "Blocked tasks require a reason.")
        if (
            status in {Task.Status.DONE, Task.Status.REVIEW}
            and not data.get("result_summary", "").strip()
        ):
            self.add_error("result_summary", "Review and done require a result summary.")
        return data


class TaskEventInline(admin.TabularInline):
    model = TaskEvent
    extra = 0
    can_delete = False
    readonly_fields = ("event_type", "agent", "message", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    form = TaskAdminForm
    list_display = ("title", "project", "status", "priority", "claimed_by", "heartbeat_at")
    list_filter = ("status", "project", "claimed_by")
    search_fields = ("title", "description")
    filter_horizontal = ("depends_on",)
    readonly_fields = ("claimed_at", "heartbeat_at", "created_by", "created_at", "updated_at")
    inlines = [TaskEventInline]
    actions = None

    def has_delete_permission(self, request, obj=None):
        return False

    @transaction.atomic
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST":
            services.lock_admin_edits()
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except services.TaskError as exc:
            return HttpResponse(
                f"Task changed while editing: {exc.message}. Reload and retry.",
                status=409,
                content_type="text/plain",
            )

    def save_model(self, request, obj, form, change):
        current = services.admin_save_task(
            obj,
            form.changed_data,
            form.cleaned_data.get("depends_on", []),
            request.user.get_username(),
        )
        for field in Task._meta.concrete_fields:
            setattr(obj, field.attname, getattr(current, field.attname))
        obj._state.adding = False

    def save_related(self, request, form, formsets, change):
        # The service saved dependencies and audit together; avoid a stale form.save_m2m().
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ("task", "event_type", "agent", "created_at")
    list_filter = ("event_type",)
    readonly_fields = ("task", "agent", "event_type", "message", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.site_header = "Agent Task Coordinator"
admin.site.site_title = "Task Coordinator"


@admin.register(WorkerCommand)
class WorkerCommandAdmin(admin.ModelAdmin):
    """Observation-first admin; staff resolve ambiguous deliveries via the API."""

    list_display = (
        "id",
        "worker",
        "action",
        "status",
        "delivery_uncertain",
        "created_by_label",
        "created_at",
    )
    list_filter = ("status", "action", "delivery_uncertain")
    search_fields = ("worker__name", "remote_session_id", "client_request_id", "actor_label")
    readonly_fields = (
        "worker",
        "created_by",
        "actor_label",
        "action",
        "payload",
        "remote_session_id",
        "session",
        "request_id",
        "client_request_id",
        "status",
        "delivery_uncertain",
        "dispatched_at",
        "completed_at",
        "result",
        "error",
        "uncertain_text",
        "created_at",
    )
    actions = ["purge_terminal"]

    @admin.display(description="Actor")
    def created_by_label(self, obj):
        return obj.actor_label or (obj.created_by.name if obj.created_by else "")

    @admin.action(description="Delete resolved commands (terminal only)")
    def purge_terminal(self, request, queryset):
        removed, _ = queryset.exclude(status="queued").exclude(status="dispatched").delete()
        self.message_user(request, f"Deleted {removed} resolved command(s).")
