from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView

from coordinator.dashboard import dashboard, dashboard_state, workspace_state

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    path("dashboard/", dashboard, name="dashboard"),
    path("dashboard/state/", dashboard_state, name="dashboard-state"),
    path("dashboard/workspace/", workspace_state, name="workspace-state"),
    path("healthz", lambda request: JsonResponse({"status": "ok"})),
    path("admin/", admin.site.urls),
    # Versioned contract for web/mobile clients; /api/ remains a compatibility alias
    # for MCP and bridges and resolves to the same views.
    path("api/v1/schema", SpectacularAPIView.as_view(), name="openapi-schema"),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include(("coordinator.urls", "coordinator"), namespace="v1")),
    path("api/", include("coordinator.urls")),
    path("api-auth/", include("rest_framework.urls")),
]
