from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from .dashboard_api import DashboardStateView, WorkspaceStateView
from .frontend import dashboard_assets


@never_cache
@staff_member_required
@ensure_csrf_cookie
def dashboard(request):
    return render(
        request,
        "coordinator/dashboard.html",
        {"demo": settings.COORDINATOR_DEMO, "assets": dashboard_assets()},
    )


# Legacy routes kept as thin aliases of the /api/v1/dashboard/ views (#40) until the
# SPA split removes them. staff_member_required preserves their login redirect.
dashboard_state = never_cache(staff_member_required(DashboardStateView.as_view()))
workspace_state = never_cache(staff_member_required(WorkspaceStateView.as_view()))
