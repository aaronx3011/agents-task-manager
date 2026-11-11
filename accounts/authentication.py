from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .services import authenticate_token


class UserTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Token <key>` for mobile devices; agents keep `Bearer` keys."""

    keyword = "Token"

    def authenticate(self, request):
        parts = authentication.get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"token":
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Invalid Token authorization header")
        try:
            raw = parts[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise exceptions.AuthenticationFailed("Invalid token") from exc
        token = authenticate_token(raw)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid or revoked token")
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword


class UserTokenScheme(OpenApiAuthenticationExtension):
    target_class = "accounts.authentication.UserTokenAuthentication"
    name = "deviceToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "Per-device user token: `Authorization: Token <key>`.",
        }
