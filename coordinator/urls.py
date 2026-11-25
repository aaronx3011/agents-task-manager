from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .chat_rooms import (
    AdvanceView,
    ChatModelsView,
    MessagesView,
    ParticipantDetailView,
    ParticipantsView,
    RoomDetailView,
    RoomsView,
)
from .dashboard_api import DashboardStateView, MetaView, WorkspaceStateView
from .planning_api import (
    planning_report,
    project_pipe,
    project_pipes,
    project_planning,
    projects_create,
)
from .views import (
    AgentRegistrationViewSet,
    CommandViewSet,
    TaskViewSet,
    WorkerViewSet,
    attempt_link,
    interactions_read,
    overview,
    sessions_read,
    worker_sync,
)
from .voice import VoiceSessionDetailView, VoiceSessionView, VoiceStatusView, VoiceToolView
from .voice_runtime import VoiceChatView, VoiceSpeechView, VoiceTranscriptionView

router = DefaultRouter(trailing_slash=False)
router.register("agents", AgentRegistrationViewSet, basename="agent-registration")
router.register("tasks", TaskViewSet, basename="task")
router.register("workers", WorkerViewSet, basename="worker")
router.register("commands", CommandViewSet, basename="command")

urlpatterns = [
    path("voice/status", VoiceStatusView.as_view(), name="voice-status"),
    path("voice/chat", VoiceChatView.as_view(), name="voice-chat"),
    path("voice/transcriptions", VoiceTranscriptionView.as_view(), name="voice-transcription"),
    path("voice/speech", VoiceSpeechView.as_view(), name="voice-speech"),
    path("voice/sessions", VoiceSessionView.as_view(), name="voice-sessions"),
    path("voice/sessions/<uuid:pk>", VoiceSessionDetailView.as_view(), name="voice-session"),
    path("voice/tools/<str:operation>", VoiceToolView.as_view(), name="voice-tool"),
    path("chat/models", ChatModelsView.as_view(), name="chat-models"),
    path("chat/rooms", RoomsView.as_view(), name="chat-rooms"),
    path("chat/rooms/<int:pk>", RoomDetailView.as_view(), name="chat-room"),
    path("chat/rooms/<int:pk>/participants", ParticipantsView.as_view(), name="chat-participants"),
    path(
        "chat/rooms/<int:pk>/participants/<int:participant_id>",
        ParticipantDetailView.as_view(),
        name="chat-participant",
    ),
    path("chat/rooms/<int:pk>/messages", MessagesView.as_view(), name="chat-messages"),
    path("chat/rooms/<int:pk>/advance", AdvanceView.as_view(), name="chat-advance"),
    path("projects", projects_create, name="projects-create"),
    path("planning/jobs/<int:pk>/report", planning_report, name="planning-report"),
    path("projects/<slug:slug>/planning", project_planning, name="project-planning"),
    path("projects/<slug:slug>/pipes", project_pipes, name="project-pipes"),
    path("projects/<slug:slug>/pipes/<int:pipe_id>", project_pipe, name="project-pipe"),
    path("overview", overview, name="overview"),
    path("meta", MetaView.as_view(), name="meta"),
    path("dashboard/state", DashboardStateView.as_view(), name="api-dashboard-state"),
    path("dashboard/workspace", WorkspaceStateView.as_view(), name="api-dashboard-workspace"),
    path("sessions", sessions_read, name="sessions"),
    path("interactions", interactions_read, name="interactions"),
    path("worker/sync", worker_sync, name="worker-sync"),
    path("attempts/<int:pk>/link", attempt_link, name="attempt-link"),
    path("", include(router.urls)),
]
