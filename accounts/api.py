"""Session auth for the web client (#45): csrf, login, logout, me.

Django sessions + CSRF only; no third-party auth packages or external services.
Mobile clients use per-device tokens instead (#48).
"""

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from coordinator.auth import IsViewer

from .models import Role
from .services import role_of

INVALID_LOGIN = "Invalid username or password."


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=4096, trim_whitespace=False, write_only=True)


class MeSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="pk")
    username = serializers.CharField(source="get_username")
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Role.choices)
    is_superuser = serializers.BooleanField()


CSRF_RESPONSE = inline_serializer("CsrfToken", {"csrfToken": serializers.CharField()})
LOGIN_ERROR = inline_serializer("LoginError", {"detail": serializers.CharField()})


def me_payload(user):
    user.role = role_of(user)
    return MeSerializer(user).data


def enforce_csrf(request):
    # DRF only checks CSRF for already-authenticated sessions; login/logout must too.
    SessionAuthentication().enforce_csrf(request)


class LoginThrottle(SimpleRateThrottle):
    """Attempts per client address and username, counted in the shared cache."""

    scope = "login"

    def get_cache_key(self, request, view):
        username = str(request.data.get("username", "")).strip().lower()[:150]
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{self.get_ident(request)}:{username}",
        }


class PublicAuthView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [permissions.AllowAny]


@method_decorator([never_cache, ensure_csrf_cookie], name="dispatch")
class CsrfView(PublicAuthView):
    @extend_schema(
        operation_id="auth_csrf",
        summary="Set the csrftoken cookie and return the token for the X-CSRFToken header",
        responses=CSRF_RESPONSE,
    )
    def get(self, request):
        return Response({"csrfToken": get_token(request)})


@method_decorator(never_cache, name="dispatch")
class LoginView(PublicAuthView):
    throttle_classes = [LoginThrottle]

    @extend_schema(
        operation_id="auth_login",
        summary="Start a browser session (requires X-CSRFToken)",
        request=LoginSerializer,
        responses={200: MeSerializer, 400: LOGIN_ERROR, 403: LOGIN_ERROR, 429: LOGIN_ERROR},
    )
    def post(self, request):
        enforce_csrf(request)
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        # ModelBackend returns None for inactive users: same answer as a bad password.
        if user is None or role_of(user) is None:
            return Response({"detail": INVALID_LOGIN}, status=status.HTTP_400_BAD_REQUEST)
        login(request, user)  # Rotates the session key and CSRF token.
        return Response(me_payload(user))


@method_decorator(never_cache, name="dispatch")
class LogoutView(PublicAuthView):
    @extend_schema(
        operation_id="auth_logout",
        summary="End the browser session (requires X-CSRFToken)",
        request=None,
        responses={204: None},
    )
    def post(self, request):
        enforce_csrf(request)
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(never_cache, name="dispatch")
class MeView(APIView):
    permission_classes = [IsViewer]

    @extend_schema(operation_id="auth_me", summary="The signed-in user", responses=MeSerializer)
    def get(self, request):
        return Response(me_payload(request.user))
