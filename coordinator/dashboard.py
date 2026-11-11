from functools import wraps

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from accounts.services import role_of

from .dashboard_api import DashboardStateView, WorkspaceStateView
from .frontend import dashboard_assets


def role_required(view):
    """Any role may open the dashboard; anonymous users go to the login page.

    Replaces staff_member_required (#43) for the legacy JSON aliases, which go
    away with the SPA split (#50).
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), "admin:login", REDIRECT_FIELD_NAME)
        if role_of(request.user) is None:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


@never_cache
@ensure_csrf_cookie
def dashboard(request):
    """The SPA shell. It holds no data: the app signs in through /api/v1/auth (#49)
    and every data endpoint checks the session or token itself."""
    return render(
        request,
        "coordinator/dashboard.html",
        {"demo": settings.COORDINATOR_DEMO, "assets": dashboard_assets()},
    )


# Legacy routes kept as thin aliases of the /api/v1/dashboard/ views (#40) until the
# SPA split removes them. role_required keeps their login redirect.
dashboard_state = never_cache(role_required(DashboardStateView.as_view()))
workspace_state = never_cache(role_required(WorkspaceStateView.as_view()))
