"""The outbox: one `ext_events` row per change, written in the transaction that
makes the change.

`emit` only adds the row to the session. The caller's own commit persists it
together with the change, so there is an event exactly when the change
committed. After that commit the caller calls `dispatch`, which asks Celery to
deliver what is owed; if that never happens (the broker is down, the process
dies) the beat schedule runs the same delivery task, and the gateway can always
catch up through `GET /ext/events?since=`.
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from .models import ExtEvent

log = logging.getLogger("freeframe.ext.events")

TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_STATUS_CHANGED = "task.status_changed"
TASK_NOTE_CREATED = "task_note.created"
COMMENT_CREATED = "comment.created"
COMMENT_VISIBILITY_CHANGED = "comment.visibility_changed"
VERSION_CREATED = "version.created"
VERSION_PUBLISHED = "version.published"

# Arbitrary, fixed: the advisory lock that orders event writers.
_SEQ_LOCK_KEY = 0x4646_4558_54  # "FFEXT"

EVENT_TYPES = (
    TASK_CREATED, TASK_UPDATED, TASK_STATUS_CHANGED, TASK_NOTE_CREATED,
    COMMENT_CREATED, COMMENT_VISIBILITY_CHANGED, VERSION_CREATED, VERSION_PUBLISHED,
)


def _plain(value):
    """JSON-safe copy of a payload value."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value
    return value


def emit(db, event_type: str, project_id, payload: dict) -> Optional[ExtEvent]:
    """Add one event to the session. Never commits."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    # `seq` must rise in commit order, or a gateway that has read up to N could
    # miss an N-1 that committed later. A sequence alone does not give that: two
    # transactions take their numbers when they insert, not when they commit. So
    # the first event in a transaction takes a lock the next writer waits on
    # until this one commits or rolls back.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SEQ_LOCK_KEY})
    event = ExtEvent(type=event_type, project_id=project_id, payload=_plain(payload))
    db.add(event)
    db.flush()
    return event


def dispatch() -> None:
    """Ask the worker to deliver undelivered events. Call after the commit.

    Best-effort: a failure here loses nothing, because the rows are committed
    and the scheduled run of the same task delivers them.
    """
    from ..config import settings
    if not getattr(settings, "ext_gateway_webhook_url", ""):
        return
    try:
        from ..tasks.celery_app import send_task_safe
        from .webhook import deliver_ext_events
        send_task_safe(deliver_ext_events)
    except Exception:
        log.warning("could not queue ext event delivery", exc_info=True)


# ── Payloads ───────────────────────────────────────────────────────────────────

def comment_payload(comment, asset) -> dict:
    return {
        "comment_id": comment.id,
        "asset_id": asset.id,
        "version_id": comment.version_id,
        "parent_id": comment.parent_id,
        "author_id": comment.author_id,
        "guest_author_id": comment.guest_author_id,
        "visibility": comment.visibility,
        "timecode_start": comment.timecode_start,
        "timecode_end": comment.timecode_end,
        "body": comment.body,
    }


def version_payload(version, asset) -> dict:
    return {
        "version_id": version.id,
        "asset_id": asset.id,
        "version_number": version.version_number,
        "visibility": version.visibility,
        "created_by": version.created_by,
    }


def emit_version_created(db, version) -> None:
    """`version.created` for an upload that has just been completed."""
    from ..models.asset import Asset
    project_id = db.query(Asset.project_id).filter(Asset.id == version.asset_id).scalar()
    if project_id is None:
        return
    emit(db, VERSION_CREATED, project_id, {
        "version_id": version.id,
        "asset_id": version.asset_id,
        "version_number": version.version_number,
        "visibility": getattr(version, "visibility", None),
        "created_by": version.created_by,
    })
