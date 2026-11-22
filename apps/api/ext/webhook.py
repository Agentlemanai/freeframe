"""Deliver the outbox to the gateway as signed webhooks.

One event per request, oldest first, and the next one is not sent until the
gateway has answered 2xx for the one before it. The gateway therefore sees
events in `seq` order, and a redelivery is recognisable by its `seq`.

A failed delivery stops the run and backs off (5 s doubling to 15 min). The
task runs again from the beat schedule every minute, so a gateway that was down
catches up by itself. An event the gateway keeps refusing is given up on after
MAX_ATTEMPTS so it cannot hold up everything behind it; it stays in the table,
and `GET /ext/events?since=` still returns it.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import settings
from ..database import SessionLocal
from ..tasks.celery_app import celery_app
from .models import ExtEvent

log = logging.getLogger("freeframe.ext.webhook")

MAX_ATTEMPTS = 12
BATCH = 100
TIMEOUT_SECONDS = 10.0
SIGNATURE_HEADER = "X-FreeFrame-Signature"
_LOCK_KEY = "ext:webhook:lock"
_BACKOFF_KEY = "ext:webhook:not-before"


def event_body(event: ExtEvent) -> bytes:
    """The exact bytes that are sent and signed."""
    return json.dumps({
        "seq": event.seq,
        "type": event.type,
        "project_id": str(event.project_id),
        "payload": event.payload,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(body: bytes, secret: str) -> str:
    """`sha256=<hex>`: HMAC-SHA256 of the request body with the shared secret."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def backoff_seconds(attempts: int) -> int:
    return min(5 * 2 ** max(0, attempts - 1), 900)


def _post(url: str, body: bytes, headers: dict) -> int:
    response = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
    return response.status_code


def deliver_pending(db, *, post=_post, redis=None) -> dict:
    """Send what is owed, in order. Returns counts; never raises for a refusal.

    `post` and `redis` are parameters so the tests can run this against the
    real database without a gateway or a broker.
    """
    url = settings.ext_gateway_webhook_url
    secret = settings.ext_gateway_webhook_secret
    if not url:
        return {"sent": 0, "skipped": "no webhook url"}
    if not secret:
        # Unsigned events would teach the gateway to accept unsigned events.
        log.error("EXT_GATEWAY_WEBHOOK_SECRET is not set; ext events are not being delivered")
        return {"sent": 0, "skipped": "no webhook secret"}
    if redis is not None and redis.get(_BACKOFF_KEY):
        return {"sent": 0, "skipped": "backing off"}

    sent = 0
    while True:
        events = (
            db.query(ExtEvent)
            .filter(ExtEvent.delivered_at.is_(None), ExtEvent.attempts < MAX_ATTEMPTS)
            .order_by(ExtEvent.seq)
            .limit(BATCH)
            .all()
        )
        if not events:
            return {"sent": sent}
        for event in events:
            body = event_body(event)
            headers = {
                "Content-Type": "application/json",
                SIGNATURE_HEADER: sign(body, secret),
                "X-FreeFrame-Event": event.type,
                "X-FreeFrame-Seq": str(event.seq),
            }
            try:
                status_code = post(url, body, headers)
                ok = 200 <= status_code < 300
                problem = f"HTTP {status_code}"
            except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
                ok, problem = False, type(exc).__name__
            event.attempts += 1
            if ok:
                event.delivered_at = datetime.now(timezone.utc)
                db.commit()
                sent += 1
                continue
            db.commit()
            wait = backoff_seconds(event.attempts)
            if event.attempts >= MAX_ATTEMPTS:
                log.error("ext event %s given up on after %s attempts (%s)",
                          event.seq, event.attempts, problem)
            else:
                log.warning("ext event %s not delivered (%s); retrying in %ss",
                            event.seq, problem, wait)
            if redis is not None:
                redis.set(_BACKOFF_KEY, "1", ex=wait)
            return {"sent": sent, "failed_seq": event.seq, "retry_in": wait}


@celery_app.task(name="deliver_ext_events")
def deliver_ext_events():
    from ..services.event_service import _get_sync_redis
    redis = _get_sync_redis()
    # One deliverer at a time, or two runs would interleave and break the order.
    lock = redis.lock(_LOCK_KEY, timeout=300, blocking=False)
    if not lock.acquire(blocking=False):
        return {"sent": 0, "skipped": "another run holds the lock"}
    db = SessionLocal()
    try:
        return deliver_pending(db, redis=redis)
    finally:
        db.close()
        try:
            lock.release()
        except Exception:  # the lock timed out; nothing to release
            pass
