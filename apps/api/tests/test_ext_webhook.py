"""Webhook delivery of the event outbox (apps/api/ext/webhook.py): signed, in
order, retried with backoff, and never ahead of an event that failed."""
import hashlib
import hmac
import json

import pytest

from apps.api.ext import events, webhook
from apps.api.ext.models import ExtEvent

SECRET = "test-shared-secret"
URL = "https://gateway.test/freeframe/events"


class FakeRedis:
    def __init__(self):
        self.keys = {}

    def get(self, key):
        return self.keys.get(key)

    def set(self, key, value, ex=None):
        self.keys[key] = (value, ex)


@pytest.fixture
def outbox(live_db, seed, monkeypatch):
    from apps.api.config import settings
    monkeypatch.setattr(settings, "ext_gateway_webhook_url", URL)
    monkeypatch.setattr(settings, "ext_gateway_webhook_secret", SECRET)
    project = seed.project(seed.user())

    def add(n):
        made = [events.emit(live_db, events.TASK_CREATED, project.id, {"n": i}) for i in range(n)]
        live_db.flush()
        return [e.seq for e in made]
    return add


def _gateway(answers):
    """A gateway that answers each request with the next of `answers`."""
    received = []
    script = list(answers)

    def post(url, body, headers):
        received.append((url, body, headers))
        answer = script.pop(0) if script else 200
        if isinstance(answer, Exception):
            raise answer
        return answer
    post.received = received
    return post


def test_events_are_signed_and_delivered_in_order(live_db, outbox):
    seqs = outbox(3)
    gateway = _gateway([200, 204, 200])
    assert webhook.deliver_pending(live_db, post=gateway) == {"sent": 3}
    assert [json.loads(body)["seq"] for _, body, _ in gateway.received] == seqs
    for url, body, headers in gateway.received:
        assert url == URL
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-FreeFrame-Signature"] == expected
        assert headers["X-FreeFrame-Seq"] == str(json.loads(body)["seq"])
        assert headers["X-FreeFrame-Event"] == "task.created"
    assert live_db.query(ExtEvent).filter(ExtEvent.delivered_at.is_(None)).count() == 0
    # Nothing owed: nothing sent.
    assert webhook.deliver_pending(live_db, post=gateway) == {"sent": 0}
    assert len(gateway.received) == 3


@pytest.mark.parametrize("failure", [500, 404, ConnectionError("gateway is down"), TimeoutError()])
def test_a_failure_stops_the_run_so_nothing_overtakes_it(live_db, outbox, failure):
    seqs = outbox(3)
    gateway = _gateway([200, failure])
    redis = FakeRedis()
    result = webhook.deliver_pending(live_db, post=gateway, redis=redis)
    assert result == {"sent": 1, "failed_seq": seqs[1], "retry_in": 5}
    assert len(gateway.received) == 2                         # the third was never attempted
    rows = {e.seq: e for e in live_db.query(ExtEvent).filter(ExtEvent.seq.in_(seqs))}
    assert rows[seqs[0]].delivered_at is not None
    assert rows[seqs[1]].delivered_at is None and rows[seqs[1]].attempts == 1
    assert rows[seqs[2]].attempts == 0
    assert redis.keys[webhook._BACKOFF_KEY] == ("1", 5)

    # While backing off, a run does nothing.
    assert webhook.deliver_pending(live_db, post=gateway, redis=redis)["skipped"] == "backing off"
    assert len(gateway.received) == 2
    # After it: the same event again, same seq, then the rest.
    redis.keys.clear()
    assert webhook.deliver_pending(live_db, post=gateway, redis=redis) == {"sent": 2}
    assert [json.loads(b)["seq"] for _, b, _ in gateway.received] == [seqs[0], seqs[1], seqs[1], seqs[2]]


def test_the_wait_between_retries_doubles_up_to_fifteen_minutes():
    assert [webhook.backoff_seconds(n) for n in (1, 2, 3, 4, 8, 9, 30)] == [5, 10, 20, 40, 640, 900, 900]


def test_an_event_the_gateway_keeps_refusing_is_given_up_on_and_stays_readable(live_db, outbox):
    seqs = outbox(2)
    gateway = _gateway([400] * webhook.MAX_ATTEMPTS + [200])
    for _ in range(webhook.MAX_ATTEMPTS):
        webhook.deliver_pending(live_db, post=gateway)
    stuck = live_db.query(ExtEvent).filter(ExtEvent.seq == seqs[0]).one()
    assert stuck.attempts == webhook.MAX_ATTEMPTS and stuck.delivered_at is None
    # It no longer holds up what came after it...
    assert webhook.deliver_pending(live_db, post=gateway) == {"sent": 1}
    assert json.loads(gateway.received[-1][1])["seq"] == seqs[1]
    # ...and the catch-up read still has it.
    assert live_db.query(ExtEvent).filter(ExtEvent.seq > seqs[0] - 1).count() >= 2


def test_nothing_is_sent_without_a_url_or_without_a_secret(live_db, outbox, monkeypatch):
    from apps.api.config import settings

    outbox(1)
    gateway = _gateway([200])
    monkeypatch.setattr(settings, "ext_gateway_webhook_secret", "")
    assert webhook.deliver_pending(live_db, post=gateway)["skipped"] == "no webhook secret"
    monkeypatch.setattr(settings, "ext_gateway_webhook_url", "")
    assert webhook.deliver_pending(live_db, post=gateway)["skipped"] == "no webhook url"
    assert gateway.received == []
    assert live_db.query(ExtEvent).filter(ExtEvent.delivered_at.is_(None)).count() == 1


def test_the_body_that_is_signed_is_the_body_that_is_sent(live_db, outbox):
    outbox(1)
    event = live_db.query(ExtEvent).order_by(ExtEvent.seq.desc()).first()
    body = webhook.event_body(event)
    parsed = json.loads(body)
    assert set(parsed) == {"seq", "type", "project_id", "payload", "created_at"}
    assert parsed["payload"] == {"n": 0} and parsed["project_id"] == str(event.project_id)
    assert webhook.sign(body, SECRET) != webhook.sign(body + b" ", SECRET)
    assert webhook.sign(body, SECRET) != webhook.sign(body, SECRET + "x")


def test_the_delivery_task_is_registered_scheduled_and_on_a_queue_a_worker_reads():
    from apps.api.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()
    assert "deliver_ext_events" in celery_app.tasks
    assert celery_app.conf.task_routes["deliver_ext_events"] == {"queue": "email_high"}
    assert celery_app.conf.beat_schedule["deliver-ext-events"]["task"] == "deliver_ext_events"
