from rest_framework import authentication, exceptions, permissions

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


def staff_name(request):
    if get_agent(request):
        return None
    user = request.user
    return user.get_username() if user and user.is_authenticated and user.is_staff else None


class IsAgentOrStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(get_agent(request) or staff_name(request))
