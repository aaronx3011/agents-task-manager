"""Ephemeral worker observations. No model writes on the live data path."""

import asyncio
import hashlib
import json
from contextlib import suppress
from functools import wraps
from uuid import uuid4

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core.cache import cache

from .errors import TaskError
from .models import Worker, WorkerSession


def session_group(worker_id, session_id):
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    return f"live.session.{worker_id}.{digest}"


def worker_group(worker_id):
    return f"live.worker.{worker_id}"


def snapshot_key(worker_id, session_id):
    return session_group(worker_id, session_id)


def database_read(function):
    # File/shared-memory SQLite can briefly lock these reads while the HTTP
    # batch commits. Import lazily: services also imports realtime notifications.
    @wraps(function)
    def read(*args, **kwargs):
        from .services import retry_sqlite

        return retry_sqlite(function)(*args, **kwargs)

    return database_sync_to_async(read)


class WorkerLiveConsumer(AsyncJsonWebsocketConsumer):
    """Authenticate in the first frame so credentials never enter URLs/logs.

    Commands still dispatch through worker-locked, acknowledged HTTP sync. The
    socket carries only replaceable observations and post-commit wakeups.
    """

    async def connect(self):
        self.worker_id = None
        self.epoch = str(uuid4())
        self.sequence = 0
        await self.accept()
        self.authentication_timeout = asyncio.create_task(self.expire_unauthenticated())

    async def expire_unauthenticated(self):
        await asyncio.sleep(5)
        if self.worker_id is None:
            await self.close(code=4401)

    async def disconnect(self, close_code):
        self.authentication_timeout.cancel()
        with suppress(asyncio.CancelledError):
            await self.authentication_timeout
        if self.worker_id:
            await self.channel_layer.group_discard(worker_group(self.worker_id), self.channel_name)

    @database_read
    def authenticate(self, content):
        name, key = content.get("worker"), content.get("key")
        if not isinstance(name, str) or not isinstance(key, str) or not 32 <= len(key) <= 128:
            return False
        worker = Worker.objects.select_related("agent").filter(name=name, is_enabled=True).first()
        if not worker or not worker.agent or not worker.agent.is_active:
            return False
        if not check_password(key, worker.agent.api_key_hash):
            return False
        self.worker_id = worker.pk
        self.worker_name = worker.name
        self.agent_id = worker.agent_id
        self.key_hash = worker.agent.api_key_hash
        return True

    @database_read
    def authorized(self, session_id=None):
        valid = Worker.objects.filter(
            pk=self.worker_id,
            is_enabled=True,
            agent_id=self.agent_id,
            agent__is_active=True,
            agent__api_key_hash=self.key_hash,
        ).exists()
        if session_id is not None:
            valid = (
                valid
                and WorkerSession.objects.filter(
                    worker_id=self.worker_id, remote_id=session_id
                ).exists()
            )
        return valid

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if text_data is None or len(text_data.encode()) > settings.WORKER_LIVE_MAX_BYTES:
            await self.close(code=4400)
            return
        try:
            content = json.loads(text_data)
        except (ValueError, TypeError):
            await self.close(code=4400)
            return
        if not isinstance(content, dict):
            await self.close(code=4400)
            return
        await self.receive_json(content)

    async def receive_json(self, content, **kwargs):
        from .services import normalize_message_record

        if self.worker_id is None:
            if content.get("type") != "authenticate" or not await self.authenticate(content):
                await self.close(code=4403)
                return
            self.authentication_timeout.cancel()
            await self.channel_layer.group_add(worker_group(self.worker_id), self.channel_name)
            await self.send_json({"type": "ready", "protocol_version": 1})
            return
        session_id = content.get("sessionID")
        if not await self.authorized():
            await self.close(code=4403)
            return
        if content.get("type") == "ping":
            await self.channel_layer.group_add(worker_group(self.worker_id), self.channel_name)
            await self.send_json({"type": "pong"})
            return
        if content.get("type") not in {"snapshot", "gap"}:
            await self.close(code=4400)
            return
        records = content.get("messages", [])
        if (
            not isinstance(session_id, str)
            or not 1 <= len(session_id) <= 128
            or not isinstance(records, list)
            or len(records) > 300
            or not await self.authorized(session_id)
        ):
            await self.close(code=4400)
            return
        if content["type"] == "gap":
            try:
                await database_sync_to_async(cache.delete)(snapshot_key(self.worker_id, session_id))
                await self.channel_layer.group_send(
                    session_group(self.worker_id, session_id),
                    {
                        "type": "session.live",
                        "snapshot": {
                            "type": "session.gap",
                            "worker": self.worker_name,
                            "sessionID": session_id,
                        },
                    },
                )
            except Exception:
                await self.close(code=1013)
            return
        try:
            messages = [normalize_message_record(record) for record in records]
        except TaskError:
            await self.close(code=4400)
            return
        self.sequence += 1
        snapshot = {
            "type": "session.live",
            "worker": self.worker_name,
            "sessionID": session_id,
            "epoch": self.epoch,
            "sequence": self.sequence,
            "executionStatus": str(content.get("executionStatus") or "")[:32],
            "messages": [
                {
                    "remoteID": message["remote_id"],
                    "role": message["role"],
                    "remoteTime": message["remote_time"],
                    "content": message["content"],
                    "parts": message["parts"],
                }
                for message in messages
            ],
        }
        try:
            await database_sync_to_async(cache.set)(
                snapshot_key(self.worker_id, session_id), snapshot, settings.WORKER_LIVE_TTL
            )
            await self.channel_layer.group_send(
                session_group(self.worker_id, session_id),
                {"type": "session.live", "snapshot": snapshot},
            )
        except Exception:
            # The periodic HTTP reconciliation remains independent of Redis.
            await self.close(code=1013)

    async def worker_wake(self, event):
        if not await self.authorized():
            await self.close(code=4403)
            return
        await self.send_json({"type": "sync"})


@database_read
def resolve_subscription(worker, session_id):
    if not isinstance(worker, str) or not isinstance(session_id, str):
        return None
    return (
        WorkerSession.objects.filter(
            worker__name=worker, worker__is_enabled=True, remote_id=session_id
        )
        .values_list("worker_id", flat=True)
        .first()
    )
