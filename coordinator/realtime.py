from importlib import import_module
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user

GROUP = "dashboard.staff"


def publish_change(event_id=None, task_id=None, event_type=None):
    layer = get_channel_layer()
    if layer is not None:
        async_to_sync(layer.group_send)(
            GROUP,
            {
                "type": "dashboard.changed",
                "event_id": event_id,
                "task_id": task_id,
                "event_type": event_type,
            },
        )


class DashboardConsumer(AsyncJsonWebsocketConsumer):
    @database_sync_to_async
    def authorized(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            return False
        # Re-check permissions and session existence for long-lived browser connections.
        session = self.scope.get("session")
        if not session or not session.session_key:
            return False
        store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=session.session_key)
        current = get_user(SimpleNamespace(session=store))
        return bool(
            current.is_authenticated
            and current.pk == user.pk
            and current.is_active
            and current.is_staff
        )

    async def connect(self):
        if not await self.authorized():
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(GROUP, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connected"})

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if not await self.authorized():
            await self.close(code=4403)
        elif isinstance(content, dict) and content.get("type") == "ping":
            # Renew Redis group membership before its expiry on long-lived browser tabs.
            await self.channel_layer.group_add(GROUP, self.channel_name)
            await self.send_json({"type": "pong"})

    async def dashboard_changed(self, event):
        if not await self.authorized():
            await self.close(code=4403)
            return
        await self.send_json(
            {
                "type": "changed",
                "event_id": event.get("event_id"),
                "task_id": event.get("task_id"),
                "event_type": event.get("event_type"),
            }
        )
