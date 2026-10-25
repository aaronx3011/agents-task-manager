import asyncio
import logging
from importlib import import_module
from time import monotonic
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user
from django.core.cache import cache

from .live import resolve_subscription, session_group, snapshot_key, worker_group

logger = logging.getLogger(__name__)

GROUP = "dashboard.staff"
_broker_retry_after = 0


def publish_change(event_id=None, task_id=None, event_type=None, extra=None):
    global _broker_retry_after
    if monotonic() < _broker_retry_after:
        return
    layer = get_channel_layer()
    if layer is None:
        return
    message = {
        "type": "dashboard.changed",
        "event_id": event_id,
        "task_id": task_id,
        "event_type": event_type,
    }
    if extra:
        message.update(extra)

    async def send():
        await asyncio.wait_for(layer.group_send(GROUP, message), timeout=1)
        if extra and event_type == "command.queued":
            await asyncio.wait_for(
                layer.group_send(worker_group(extra["worker_id"]), {"type": "worker.wake"}),
                timeout=1,
            )

    try:
        async_to_sync(send)()
    except Exception:  # broker failure mutates nothing already committed
        # A paused broker must not stall every post-commit callback in a sync.
        _broker_retry_after = monotonic() + 5
        logger.warning("Dashboard notification failed; committed state unchanged", exc_info=True)


def dispatch_notification(
    event_type, subject, worker=None, session=None, command=None, event_id=None
):
    """Post-commit worker-domain invalidation (#11/#15)."""
    publish_change(
        event_id=event_id,
        event_type=event_type,
        extra={
            "subject": subject,
            "worker_id": worker.pk if worker else None,
            "session_id": getattr(session, "pk", None),
            "command_id": getattr(command, "pk", None),
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
        self.subscription = None
        if not await self.authorized():
            await self.close(code=4403)
            return
        await self.channel_layer.group_add(GROUP, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connected"})

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP, self.channel_name)
        if self.subscription:
            await self.channel_layer.group_discard(self.subscription[0], self.channel_name)

    async def receive_json(self, content, **kwargs):
        if not await self.authorized():
            await self.close(code=4403)
        elif isinstance(content, dict) and content.get("type") == "ping":
            # Renew Redis group membership before its expiry on long-lived browser tabs.
            await self.channel_layer.group_add(GROUP, self.channel_name)
            if self.subscription:
                await self.channel_layer.group_add(self.subscription[0], self.channel_name)
            await self.send_json({"type": "pong"})
        elif isinstance(content, dict) and content.get("type") == "subscribe":
            if self.subscription:
                await self.channel_layer.group_discard(self.subscription[0], self.channel_name)
                self.subscription = None
            worker, session_id = content.get("worker"), content.get("sessionID")
            if worker is None and session_id is None:
                return
            worker_id = await resolve_subscription(worker, session_id)
            if worker_id is None:
                await self.send_json({"type": "subscription.error"})
                return
            group = session_group(worker_id, session_id)
            self.subscription = (group, worker, session_id)
            # Join before reading cache; monotonic sequence checks on the client
            # absorb a simultaneous group delivery without regressing the view.
            await self.channel_layer.group_add(group, self.channel_name)
            await self.send_json({"type": "subscribed", "worker": worker, "sessionID": session_id})
            try:
                snapshot = await database_sync_to_async(cache.get)(
                    snapshot_key(worker_id, session_id)
                )
            except Exception:
                snapshot = None
            if snapshot:
                await self.session_live({"snapshot": snapshot})

    async def session_live(self, event):
        if not await self.authorized():
            await self.close(code=4403)
            return
        snapshot = event["snapshot"]
        if self.subscription and self.subscription[1:] == (
            snapshot["worker"],
            snapshot["sessionID"],
        ):
            if snapshot["type"] == "session.live":
                messages = sorted(
                    snapshot["messages"],
                    key=lambda message: (message.get("remoteTime") or 0, message["remoteID"]),
                )
                snapshot = {
                    **snapshot,
                    "messages": messages[-10:],
                    "hasEarlier": len(messages) > 10,
                }
            await self.send_json(snapshot)

    async def dashboard_changed(self, event):
        if not await self.authorized():
            await self.close(code=4403)
            return
        event_type = event.get("event_type") or ""
        urgent = event_type.startswith(("interaction.", "permission.", "question.", "command."))
        if (
            event.get("subject") in {"workers", "sessions", "messages"}
            and not urgent
            and event_type not in {"session.error", "worker.synced"}
        ):
            # Keep overview traffic lightweight: one worker heartbeat per batch,
            # plus global attention/command notices. Detailed data is subscribed.
            return
        await self.send_json(
            {
                "type": "changed",
                "event_id": event.get("event_id"),
                "task_id": event.get("task_id"),
                "event_type": event.get("event_type"),
                "subject": event.get("subject"),
                "worker_id": event.get("worker_id"),
                "session_id": event.get("session_id"),
                "command_id": event.get("command_id"),
            }
        )
