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
    OwnPasswordView,
    RealtimeTicketView,
    TokenListView,
    TokenRevokeView,
    UserDetailView,
    UserListView,
    UserPasswordView,
)

app_name = "auth"

urlpatterns = [
    path("csrf", CsrfView.as_view(), name="csrf"),
    path("login", LoginView.as_view(), name="login"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("me", MeView.as_view(), name="me"),
    path("password", OwnPasswordView.as_view(), name="password"),
    path("tokens", TokenListView.as_view(), name="tokens"),
    path("tokens/<str:pk>/revoke", TokenRevokeView.as_view(), name="token-revoke"),
]

# Mounted under /api/v1/ (config/urls.py).
invite_urlpatterns = [
    path("invites", InviteListView.as_view(), name="invites"),
    path("invites/check", InviteCheckView.as_view(), name="invite-check"),
    path("invites/accept", InviteAcceptView.as_view(), name="invite-accept"),
    path("invites/<int:pk>/revoke", InviteRevokeView.as_view(), name="invite-revoke"),
    path("realtime/ticket", RealtimeTicketView.as_view(), name="realtime-ticket"),
    path("users", UserListView.as_view(), name="users"),
    path("users/<int:pk>", UserDetailView.as_view(), name="user"),
    path("users/<int:pk>/password", UserPasswordView.as_view(), name="user-password"),
]
