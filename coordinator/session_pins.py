"""Workspace-wide pins, independent of remote session execution and snapshots."""

from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth import ViewerReadsMemberWrites
from .models import WorkerSession
from .realtime import publish_change
from .services import retry_sqlite


class SessionPinInput(serializers.Serializer):
    worker = serializers.CharField(max_length=100)
    sessionID = serializers.CharField(max_length=128)
    pinned = serializers.BooleanField()


class SessionPinSerializer(serializers.Serializer):
    worker = serializers.CharField(source="worker.name")
    sessionID = serializers.CharField(source="remote_id")
    title = serializers.CharField()
    pinned_at = serializers.DateTimeField()


class SessionPinListSerializer(serializers.Serializer):
    items = SessionPinSerializer(many=True)


@method_decorator(never_cache, name="dispatch")
class SessionPinsView(APIView):
    permission_classes = [ViewerReadsMemberWrites]

    @extend_schema(responses=SessionPinListSerializer)
    def get(self, request):
        pins = WorkerSession.objects.filter(pinned_at__isnull=False).select_related("worker")
        return Response(
            {"items": SessionPinSerializer(pins.order_by("-pinned_at", "-pk"), many=True).data}
        )

    @extend_schema(request=SessionPinInput, responses=SessionPinInput)
    @retry_sqlite
    @transaction.atomic
    def post(self, request):
        data = SessionPinInput(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        session = (
            WorkerSession.objects.select_for_update()
            .filter(worker__name=values["worker"], remote_id=values["sessionID"])
            .first()
        )
        if session is None:
            raise NotFound("Unknown session for this worker")
        # Set semantics are safe to repeat; repeated pins do not change ordering.
        if bool(session.pinned_at) != values["pinned"]:
            WorkerSession.objects.filter(pk=session.pk).update(
                pinned_at=timezone.now() if values["pinned"] else None
            )
            transaction.on_commit(
                lambda: publish_change(
                    event_type="session.pins.changed", extra={"subject": "session_pins"}
                ),
                robust=True,
            )
        return Response(values)
