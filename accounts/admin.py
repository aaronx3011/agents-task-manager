from django.contrib import admin, messages

from .models import RoleChange, UserProfile
from .services import AccountError, set_role


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "updated_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email")
    readonly_fields = ("user", "created_at", "updated_at")
    fields = ("user", "role", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False  # Profiles are created with their user.

    def save_model(self, request, obj, form, change):
        # Route through the service so the edit is audited and the last owner is kept.
        try:
            set_role(obj.user, obj.role, request.user.get_username(), reason="admin edit")
        except AccountError as exc:
            obj.role = UserProfile.objects.get(pk=obj.pk).role
            messages.error(request, exc.message)


@admin.register(RoleChange)
class RoleChangeAdmin(admin.ModelAdmin):
    list_display = ("user", "old_role", "new_role", "actor", "reason", "created_at")
    list_filter = ("new_role",)
    search_fields = ("user__username", "actor")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
