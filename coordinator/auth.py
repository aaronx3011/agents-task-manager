from rest_framework import authentication, exceptions, permissions

from accounts.models import ROLE_RANK, Role
from accounts.services import role_of

from .models import Agent
from .services import retry_sqlite


class AgentKeyAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    @retry_sqlite
    def authenticate(self, request):
        parts = authentication.get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Invalid Bearer authorization header")
        try:
            raw_key = parts[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid API key") from exc
        agent = Agent.authenticate(raw_key)
        if not agent:
            raise exceptions.AuthenticationFailed("Invalid API key")
        return None, agent

    def authenticate_header(self, request):
        return self.keyword


def get_agent(request):
    return request.auth if isinstance(request.auth, Agent) else None


def human_role(request):
    """Role of the human behind a session request; None for agents and anonymous users."""
    if get_agent(request):
        return None
    return role_of(getattr(request, "user", None))


def human_actor(request):
    """Username recorded as the human actor of a mutation (any role)."""
    return request.user.get_username() if human_role(request) else None


def human_has_role(request, minimum):
    role = human_role(request)
    return role is not None and ROLE_RANK[Role(role)] >= ROLE_RANK[Role(minimum)]


class IsAgentOrRole(permissions.BasePermission):
    """Default API policy (#43).

    Agents keep their per-endpoint rules. Humans: viewers read, members write.
    """

    def has_permission(self, request, view):
        if get_agent(request):
            return True
        if request.method in permissions.SAFE_METHODS:
            return human_has_role(request, Role.VIEWER)
        return human_has_role(request, Role.MEMBER)


class HumanRolePermission(permissions.BasePermission):
    """A signed-in human with at least `minimum`; agent keys are refused."""

    minimum = Role.VIEWER

    def has_permission(self, request, view):
        return human_has_role(request, self.minimum)


class IsViewer(HumanRolePermission):
    minimum = Role.VIEWER


class IsMember(HumanRolePermission):
    minimum = Role.MEMBER


class IsOwner(HumanRolePermission):
    minimum = Role.OWNER


class ViewerReadsMemberWrites(permissions.BasePermission):
    """Human-only endpoints: viewers read, members write; agent keys are refused."""

    def has_permission(self, request, view):
        minimum = Role.VIEWER if request.method in permissions.SAFE_METHODS else Role.MEMBER
        return human_has_role(request, minimum)
