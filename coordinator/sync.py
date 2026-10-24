"""POST /api/worker/sync envelope validation and ingestion (#7/#11).

Contract lives in GitHub issue #1. Validation is bounded and rejects whole
batches: the bridge re-drives with bounded buffers, so atomicity rules are
fail-fast rather than per-record salvage. Foreign references reject the entire
sync with 400; oversized sections reject with 413; unsupported protocol majors
reject with 410. Extension points implemented by follow-up issues:
- command results: coordinator services.apply_command_results (#12)
- pending interactions: coordinator services.ingest_pending_interactions (#14)
- command dispatch in the sync response: services.dispatch_queued_commands (#12)
"""

from django.conf import settings

from .errors import TaskError
from .models import WorkerEvent

PROTOCOL_VERSION = 1

MAX_SESSIONS = 200
MAX_UPDATES = 100
MAX_MESSAGES_PER_SESSION = 300
MAX_EVENTS = 1000
MAX_RESULTS = 200
MAX_PENDING_REQUESTS = 200


def _record(value, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskError(f"{name} must be an object", 400)
    return value


def _list(value, name, limit):
    if value is None:
        return []
    if not isinstance(value, list):
        raise TaskError(f"{name} must be an array", 400)
    if len(value) > limit:
        raise TaskError(f"{name} exceeds the limit of {limit} entries", 413)
    return value


def _identifier(value, name, limit=128):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise TaskError(f"{name} must be a non-empty string of at most {limit} characters", 400)
    return value


def parse_sync_payload(payload):
    """Validate one sync envelope; returns a typed dict for services.apply_worker_sync."""
    payload = _record(payload, "payload")
    missing = [name for name in ("worker", "protocol_version", "meta") if name not in payload]
    if missing:
        raise TaskError(f"Sync payload is missing {', '.join(missing)}", 400)
    worker_name = _identifier(payload["worker"], "worker", 100)
    version = payload["protocol_version"]
    if not isinstance(version, int) or version < 1:
        raise TaskError("protocol_version must be a positive integer", 400)
    if version != getattr(settings, "WORKER_SYNC_PROTOCOL_VERSION", PROTOCOL_VERSION):
        raise TaskError(
            f"Unsupported sync protocol version {version}; the server supports "
            f"{getattr(settings, 'WORKER_SYNC_PROTOCOL_VERSION', PROTOCOL_VERSION)} "
            "and additive minor fields. Reprovision or upgrade the bridge.",
            410,
        )
    meta = _record(payload["meta"], "meta")
    sessions = _list(payload.get("sessions"), "sessions", MAX_SESSIONS)
    raw_updates = _list(payload.get("updates"), "updates", MAX_UPDATES)
    updates = []
    for update in raw_updates:
        update = _record(update, "updates entry")
        session_id = _identifier(update.get("sessionID"), "updates.sessionID")
        messages = _list(update.get("messages"), "updates.messages", MAX_MESSAGES_PER_SESSION)
        updates.append({"sessionID": session_id, "messages": messages})
    events = []
    for record in _list(payload.get("events"), "events", MAX_EVENTS):
        record = _record(record, "events entry")
        event_type = _identifier(record.get("type"), "events.type", 64)
        session_id = record.get("sessionID")
        if session_id is not None:
            session_id = _identifier(session_id, "events.sessionID")
        frame = record.get("payload")
        if frame is None:
            frame = record.get("data")
        events.append({"type": event_type, "sessionID": session_id, "frame": frame})
    results = []
    for record in _list(payload.get("results"), "results", MAX_RESULTS):
        record = _record(record, "results entry")
        results.append(
            {
                "commandID": _identifier(
                    record.get("commandID") or record.get("id"), "results.commandID", 64
                ),
                "status": record.get("status"),
                "result": record.get("result"),
                "error": record.get("error"),
            }
        )
    pending = _record(payload.get("pending"), "pending")
    permissions = _list(pending.get("permissions"), "pending.permissions", MAX_PENDING_REQUESTS)
    questions = _list(pending.get("questions"), "pending.questions", MAX_PENDING_REQUESTS)
    cursor = _record(payload.get("cursor"), "cursor")
    after = cursor.get("after")
    if after is not None and (not isinstance(after, int) or after < 0):
        raise TaskError("cursor.after must be a non-negative integer", 400)
    batch_id = cursor.get("batch_id")
    if batch_id is not None:
        _identifier(batch_id, "cursor.batch_id", 64)
    return {
        "worker": worker_name,
        "protocol_version": version,
        "meta": meta,
        "sessions": sessions,
        "updates": updates,
        "events": events,
        "results": results,
        "pending": {
            "complete": bool(pending.get("complete")),
            "permissions": permissions,
            "questions": questions,
        },
        "cursor": {"after": after, "batch_id": batch_id},
    }


def event_sequence():
    """The server's global event cursor for cursor reads and reconciliation."""
    latest = WorkerEvent.objects.order_by("pk").last()
    return latest.pk if latest is not None else 0
