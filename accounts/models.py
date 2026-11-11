from django.conf import settings
from django.db import models


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


# Higher rank includes every lower-rank capability.
ROLE_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.OWNER: 2}


def role_for_flags(is_superuser, is_staff):
    """Initial role for an account that predates roles or was created outside invites."""
    if is_superuser:
        return Role.OWNER
    if is_staff:
        return Role.MEMBER
    return Role.VIEWER


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["role"])]

    def __str__(self):
        return f"{self.user.get_username()} ({self.role})"


class RoleChange(models.Model):
    """Append-only audit of every role assignment."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_changes"
    )
    old_role = models.CharField(max_length=16, choices=Role.choices, blank=True)
    new_role = models.CharField(max_length=16, choices=Role.choices)
    # Username of the human who made the change, or a system label such as "migration".
    actor = models.CharField(max_length=150)
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.user_id}: {self.old_role or '∅'} → {self.new_role} by {self.actor}"
