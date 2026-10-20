from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView

from coordinator.dashboard import dashboard, dashboard_state, workspace_state

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    path("dashboard/", dashboard, name="dashboard"),
    path("dashboard/state/", dashboard_state, name="dashboard-state"),
    path("dashboard/workspace/", workspace_state, name="workspace-state"),
    path("healthz", lambda request: JsonResponse({"status": "ok"})),
    path("admin/", admin.site.urls),
    path("api/", include("coordinator.urls")),
    path("api-auth/", include("rest_framework.urls")),
]
