from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .planning_api import planning_report, project_planning
from .views import (
    AgentRegistrationViewSet,
    CommandViewSet,
    TaskViewSet,
    WorkerViewSet,
    attempt_link,
    overview,
    sessions_read,
    worker_sync,
)

router = DefaultRouter(trailing_slash=False)
router.register("agents", AgentRegistrationViewSet, basename="agent-registration")
router.register("tasks", TaskViewSet, basename="task")
router.register("workers", WorkerViewSet, basename="worker")
router.register("commands", CommandViewSet, basename="command")

urlpatterns = [
    path("planning/jobs/<int:pk>/report", planning_report, name="planning-report"),
    path("projects/<slug:slug>/planning", project_planning, name="project-planning"),
    path("overview", overview, name="overview"),
    path("sessions", sessions_read, name="sessions"),
    path("worker/sync", worker_sync, name="worker-sync"),
    path("attempts/<int:pk>/link", attempt_link, name="attempt-link"),
    path("", include(router.urls)),
]
