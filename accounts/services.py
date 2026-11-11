"""Role assignment. Every write goes through here so the audit row commits with it."""

from django.db import transaction

from coordinator.dbretry import retry_sqlite

from .models import ROLE_RANK, Role, RoleChange, UserProfile, role_for_flags


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def role_of(user):
    """The user's role, or None for anonymous/inactive users and agent requests."""
    if not user or not user.is_authenticated or not user.is_active:
        return None
    try:
        return user.profile.role
    except UserProfile.DoesNotExist:
        return role_for_flags(user.is_superuser, user.is_staff)


def has_role(user, minimum):
    role = role_of(user)
    return role is not None and ROLE_RANK[Role(role)] >= ROLE_RANK[Role(minimum)]


@retry_sqlite
def set_role(user, role, actor, reason=""):
    """Assign a role, auditing the change. Refuses to leave the system without an owner."""
    if role not in Role.values:
        raise AccountError(f"Unknown role: {role}")
    actor = str(actor or "").strip()
    if not actor:
        raise AccountError("A role change needs an actor")
    with transaction.atomic():
        profile = UserProfile.objects.select_for_update().filter(user=user).first()
        old_role = profile.role if profile else ""
        if old_role == role:
            return profile
        if old_role == Role.OWNER:
            others = UserProfile.objects.select_for_update().filter(role=Role.OWNER)
            if not others.exclude(pk=profile.pk).exists():
                raise AccountError("Cannot remove the last owner", 409)
        if profile is None:
            profile = UserProfile.objects.create(user=user, role=role)
        else:
            profile.role = role
            profile.save(update_fields=["role", "updated_at"])
        RoleChange.objects.create(
            user=user, old_role=old_role, new_role=role, actor=actor[:150], reason=reason[:200]
        )
    user.profile = profile
    return profile


def ensure_profile(user, actor, reason=""):
    """Give an account without a profile its initial role from its staff/superuser flags."""
    if UserProfile.objects.filter(user=user).exists():
        return UserProfile.objects.get(user=user)
    return set_role(user, role_for_flags(user.is_superuser, user.is_staff), actor, reason)
