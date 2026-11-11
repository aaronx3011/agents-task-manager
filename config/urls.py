from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView

from accounts.urls import invite_urlpatterns

# Django serves only the API, WebSockets (config/asgi.py), admin and health. The
# SPA is a separate static build served on the same origin (#50, deploy/Caddyfile).
urlpatterns = [
    path("healthz", lambda request: JsonResponse({"status": "ok"})),
    path("admin/", admin.site.urls),
    # Versioned contract for web/mobile clients; /api/ remains a compatibility alias
    # for MCP and bridges and resolves to the same views.
    path("api/v1/schema", SpectacularAPIView.as_view(), name="openapi-schema"),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include((invite_urlpatterns, "invites"))),
    path("api/v1/", include(("coordinator.urls", "coordinator"), namespace="v1")),
    path("api/", include("coordinator.urls")),
    path("api-auth/", include("rest_framework.urls")),
]
