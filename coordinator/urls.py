from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TaskViewSet, overview

router = DefaultRouter(trailing_slash=False)
router.register("tasks", TaskViewSet, basename="task")

urlpatterns = [
    path("overview", overview, name="overview"),
    path("", include(router.urls)),
]
