"""Account services. Every write goes through here so its audit row commits with it."""

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from coordinator.dbretry import retry_sqlite

from .models import (
    ROLE_RANK,
    AccountEvent,
    Invite,
    Role,
    RoleChange,
    UserProfile,
    role_for_flags,
)


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


# --- Invites (#46) -----------------------------------------------------------------

INVITE_DEFAULT_HOURS = 72
INVITE_MAX_HOURS = 24 * 30


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def record(event_type, actor_user=None, actor="", subject_user=None, message=""):
    return AccountEvent.objects.create(
        event_type=event_type,
        actor_user=actor_user,
        actor=(actor or (actor_user.get_username() if actor_user else "system"))[:150],
        subject_user=subject_user,
        message=message[:300],
    )


@retry_sqlite
def create_invite(created_by, role, hours=INVITE_DEFAULT_HOURS, note=""):
    """Return (invite, raw_token). The raw token exists only in this return value."""
    if role not in Role.values:
        raise AccountError(f"Unknown role: {role}")
    if not 1 <= int(hours) <= INVITE_MAX_HOURS:
        raise AccountError(f"Invite lifetime must be 1–{INVITE_MAX_HOURS} hours")
    raw = secrets.token_urlsafe(32)
    with transaction.atomic():
        invite = Invite.objects.create(
            role=role,
            token_hash=hash_token(raw),
            token_prefix=raw[:8],
            note=note[:200],
            created_by=created_by,
            expires_at=timezone.now() + timedelta(hours=int(hours)),
        )
        record(
            AccountEvent.Type.INVITE_CREATED,
            actor_user=created_by,
            message=f"Invite {invite.pk} ({invite.token_prefix}…) as {role}",
        )
    return invite, raw


@retry_sqlite
def revoke_invite(invite_id, actor_user):
    with transaction.atomic():
        invite = Invite.objects.select_for_update().filter(pk=invite_id).first()
        if invite is None:
            raise AccountError("Invite not found", 404)
        if invite.used_at or invite.revoked_at:
            raise AccountError(f"Invite is already {invite.status(timezone.now())}", 409)
        invite.revoked_at = timezone.now()
        invite.save(update_fields=["revoked_at"])
        record(
            AccountEvent.Type.INVITE_REVOKED,
            actor_user=actor_user,
            message=f"Invite {invite.pk} ({invite.token_prefix}…)",
        )
    return invite


def usable_invite(raw, lock=False):
    """The pending invite for a raw token, or AccountError with one generic message."""
    query = Invite.objects.filter(token_hash=hash_token(raw or ""))
    invite = (query.select_for_update() if lock else query).first()
    if invite is None or invite.status(timezone.now()) != "pending":
        # Same answer for unknown, used, revoked and expired links.
        raise AccountError("This invite link is invalid or has expired.", 410)
    return invite


@retry_sqlite
def accept_invite(raw, username, password, email=""):
    """Create the user with the invite's role and consume the invite atomically."""
    User = get_user_model()
    username = (username or "").strip()
    with transaction.atomic():
        invite = usable_invite(raw, lock=True)
        if not username or len(username) > 150:
            raise AccountError("Choose a username of 1–150 characters")
        if User.objects.filter(username__iexact=username).exists():
            raise AccountError("That username is taken", 409)
        candidate = User(username=username, email=email)
        try:
            User._meta.get_field("username").run_validators(username)
            validate_password(password, candidate)
        except ValidationError as exc:
            raise AccountError(" ".join(exc.messages)) from exc
        user = User.objects.create_user(username, email, password)
        set_role(user, invite.role, actor=f"invite:{invite.pk}", reason="invite accepted")
        invite.used_at = timezone.now()
        invite.used_by = user
        invite.save(update_fields=["used_at", "used_by"])
        record(
            AccountEvent.Type.INVITE_ACCEPTED,
            actor=username,
            actor_user=user,
            subject_user=user,
            message=f"Invite {invite.pk} ({invite.token_prefix}…) as {invite.role}",
        )
    return user
