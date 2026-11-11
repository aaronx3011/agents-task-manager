"""Short-lived WebSocket tickets for clients without session cookies (#51).

Mobile apps authenticate HTTP with a device token. To open /ws/dashboard/ they
trade it for a single-use ticket (60s) and send it as a WebSocket subprotocol,
so neither the token nor the ticket appears in a URL or access log.
"""

import secrets

from django.core.cache import cache

from .models import UserToken
from .services import hash_token, role_of

TICKET_SECONDS = 60
SUBPROTOCOL = "atm.v1"
TICKET_PREFIX = "atm.ticket."


def issue_ticket(user, token=None):
    raw = secrets.token_urlsafe(32)
    cache.set(
        f"ws-ticket:{hash_token(raw)}",
        {"user": user.pk, "token": token.pk if isinstance(token, UserToken) else None},
        timeout=TICKET_SECONDS,
    )
    return raw


def ticket_from_subprotocols(subprotocols):
    for value in subprotocols or []:
        if value.startswith(TICKET_PREFIX):
            return value[len(TICKET_PREFIX) :]
    return None


def redeem_ticket(raw):
    """Consume a ticket once; return its claims or None."""
    if not raw or len(raw) > 128:
        return None
    key = f"ws-ticket:{hash_token(raw)}"
    claims = cache.get(key)
    # delete() reports whether this caller removed it, so a ticket works once.
    if not claims or not cache.delete(key):
        return None
    return claims


def ticket_holder_allowed(claims):
    """Re-checked on every event: user active with a role, device token not revoked."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=claims["user"]).first()
    if user is None or role_of(user) is None:
        return False
    if claims.get("token"):
        return UserToken.objects.filter(
            pk=claims["token"], user=user, revoked_at__isnull=True
        ).exists()
    return True


class TicketOrOriginValidator:
    """Browser connections must pass the Origin check (they carry cookies).

    Ticket connections come from apps that may send no Origin; their ticket was
    issued to an authenticated caller and works once, so Origin adds nothing.
    """

    def __init__(self, application, origin_validator):
        self.application = application
        self.validated = origin_validator(application)

    async def __call__(self, scope, receive, send):
        if ticket_from_subprotocols(scope.get("subprotocols")) is not None:
            return await self.application(scope, receive, send)
        return await self.validated(scope, receive, send)
