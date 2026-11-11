from django.urls import path

from .api import (
    CsrfView,
    InviteAcceptView,
    InviteCheckView,
    InviteListView,
    InviteRevokeView,
    LoginView,
    LogoutView,
    MeView,
)

app_name = "auth"

urlpatterns = [
    path("csrf", CsrfView.as_view(), name="csrf"),
    path("login", LoginView.as_view(), name="login"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("me", MeView.as_view(), name="me"),
]

# Mounted under /api/v1/ (config/urls.py).
invite_urlpatterns = [
    path("invites", InviteListView.as_view(), name="invites"),
    path("invites/check", InviteCheckView.as_view(), name="invite-check"),
    path("invites/accept", InviteAcceptView.as_view(), name="invite-accept"),
    path("invites/<int:pk>/revoke", InviteRevokeView.as_view(), name="invite-revoke"),
]
