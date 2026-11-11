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


class AccountEvent(models.Model):
    """Append-only audit of account actions: invites, user management, tokens."""

    class Type(models.TextChoices):
        INVITE_CREATED = "invite.created", "Invite created"
        INVITE_REVOKED = "invite.revoked", "Invite revoked"
        INVITE_ACCEPTED = "invite.accepted", "Invite accepted"

    event_type = models.CharField(max_length=40, choices=Type.choices)
    # The human who acted (None for anonymous invite acceptance) and their label.
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor = models.CharField(max_length=150)
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.event_type} by {self.actor}"


class Invite(models.Model):
    """Single-use invite link. Only the SHA-256 of the token is stored."""

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    # Non-secret prefix so owners can tell links apart in the list.
    token_prefix = models.CharField(max_length=8, editable=False)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_invite",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def status(self, now):
        if self.used_at:
            return "used"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return "pending"

    def __str__(self):
        return f"{self.token_prefix}… ({self.role})"
