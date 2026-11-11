"""Session auth (#45) and invite links (#46) for the web client.

Django sessions + CSRF only; no third-party auth packages or external services.
Mobile clients use per-device tokens instead (#48).
"""

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from coordinator.auth import IsOwner, IsViewer

from .models import Invite, Role
from .services import (
    INVITE_DEFAULT_HOURS,
    INVITE_MAX_HOURS,
    AccountError,
    accept_invite,
    create_invite,
    revoke_invite,
    role_of,
    usable_invite,
)

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


# --- Invites (#46) -----------------------------------------------------------------


class InviteSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="pk")
    role = serializers.ChoiceField(choices=Role.choices)
    token_prefix = serializers.CharField()
    note = serializers.CharField()
    status = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    used_at = serializers.DateTimeField(allow_null=True)
    used_by = serializers.SerializerMethodField()
    revoked_at = serializers.DateTimeField(allow_null=True)

    def get_status(self, invite) -> str:
        return invite.status(timezone.now())

    def get_created_by(self, invite) -> str | None:
        return invite.created_by.get_username() if invite.created_by_id else None

    def get_used_by(self, invite) -> str | None:
        return invite.used_by.get_username() if invite.used_by_id else None


class CreatedInviteSerializer(InviteSerializer):
    token = serializers.CharField(help_text="Shown once; only its hash is stored.")
    url = serializers.CharField(help_text="Share this link manually. Shown once.")


class InviteInput(serializers.Serializer):
    role = serializers.ChoiceField(choices=Role.choices, default=Role.MEMBER)
    expires_in_hours = serializers.IntegerField(
        min_value=1, max_value=INVITE_MAX_HOURS, default=INVITE_DEFAULT_HOURS
    )
    note = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class InviteTokenInput(serializers.Serializer):
    token = serializers.CharField(max_length=200, trim_whitespace=True)


class AcceptInviteInput(InviteTokenInput):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=4096, trim_whitespace=False, write_only=True)
    email = serializers.EmailField(required=False, allow_blank=True, default="")


INVITE_PREVIEW = inline_serializer(
    "InvitePreview",
    {
        "role": serializers.ChoiceField(choices=Role.choices),
        "expires_at": serializers.DateTimeField(),
    },
)


def account_error(exc):
    return Response({"detail": exc.message}, status=exc.status)


class InviteThrottle(SimpleRateThrottle):
    """Public invite checks/accepts per client address."""

    scope = "invite"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


@method_decorator(never_cache, name="dispatch")
class InviteListView(APIView):
    permission_classes = [IsOwner]

    @extend_schema(operation_id="invites_list", responses=InviteSerializer(many=True))
    def get(self, request):
        invites = Invite.objects.select_related("created_by", "used_by")[:200]
        return Response(InviteSerializer(invites, many=True).data)

    @extend_schema(
        operation_id="invites_create",
        summary="Create a single-use invite link (returned once)",
        request=InviteInput,
        responses={201: CreatedInviteSerializer},
    )
    def post(self, request):
        serializer = InviteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            invite, raw = create_invite(
                request.user, data["role"], data["expires_in_hours"], data["note"]
            )
        except AccountError as exc:
            return account_error(exc)
        # The token travels in the fragment so it never reaches server access logs.
        url = request.build_absolute_uri("/invite") + "#" + raw
        payload = {**InviteSerializer(invite).data, "token": raw, "url": url}
        return Response(payload, status=status.HTTP_201_CREATED)


@method_decorator(never_cache, name="dispatch")
class InviteRevokeView(APIView):
    permission_classes = [IsOwner]

    @extend_schema(operation_id="invites_revoke", request=None, responses=InviteSerializer)
    def post(self, request, pk):
        try:
            invite = revoke_invite(pk, request.user)
        except AccountError as exc:
            return account_error(exc)
        return Response(InviteSerializer(invite).data)


@method_decorator(never_cache, name="dispatch")
class InviteCheckView(PublicAuthView):
    throttle_classes = [InviteThrottle]

    @extend_schema(
        operation_id="invites_check",
        summary="Preview a pending invite before accepting (requires X-CSRFToken)",
        request=InviteTokenInput,
        responses={200: INVITE_PREVIEW, 410: LOGIN_ERROR},
    )
    def post(self, request):
        enforce_csrf(request)
        serializer = InviteTokenInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            invite = usable_invite(serializer.validated_data["token"])
        except AccountError as exc:
            return account_error(exc)
        return Response({"role": invite.role, "expires_at": invite.expires_at})


@method_decorator(never_cache, name="dispatch")
class InviteAcceptView(PublicAuthView):
    throttle_classes = [InviteThrottle]

    @extend_schema(
        operation_id="invites_accept",
        summary="Accept an invite: create the account and start a session",
        request=AcceptInviteInput,
        responses={201: MeSerializer, 400: LOGIN_ERROR, 409: LOGIN_ERROR, 410: LOGIN_ERROR},
    )
    def post(self, request):
        enforce_csrf(request)
        serializer = AcceptInviteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            user = accept_invite(data["token"], data["username"], data["password"], data["email"])
        except AccountError as exc:
            return account_error(exc)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return Response(me_payload(user), status=status.HTTP_201_CREATED)
