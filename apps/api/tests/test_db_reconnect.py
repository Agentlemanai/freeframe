"""A Postgres restart must not strand uploads at `processing`.

Workers kept dead pooled connections, so the first query of the next task raised
OperationalError before process_asset reached the handler that owns retries.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from apps.api import database
from apps.api.models.asset import ProcessingStatus
from apps.api.tasks import transcode_tasks


def _dead_connection():
    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


def _task():
    task = transcode_tasks.process_asset
    return getattr(task, "_get_current_object", lambda: task)()


def _session(*first_results):
    db = MagicMock()
    db.query.return_value = db
    db.filter.return_value = db
    db.first.side_effect = list(first_results)
    return db


def _run(retries, sessions):
    task = _task()
    task.push_request(retries=retries, called_directly=False)
    try:
        with patch.object(transcode_tasks, "SessionLocal", side_effect=sessions), \
             patch.object(task, "retry", side_effect=Retry()) as retry:
            task.run(str(uuid.uuid4()), str(uuid.uuid4()))
    except BaseException as exc:  # the assertions below say which one is right
        return retry, exc
    finally:
        task.pop_request()
    return retry, None


def test_engine_checks_connections_before_use():
    assert database.engine.pool._pre_ping is True


def test_dead_connection_during_setup_retries():
    db = _session(_dead_connection())
    retry, exc = _run(0, [db])
    retry.assert_called_once()
    assert isinstance(exc, Retry)
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_dead_connection_on_last_attempt_marks_failed():
    version = SimpleNamespace(processing_status=ProcessingStatus.processing)
    second = _session(version)
    retry, exc = _run(_task().max_retries, [_session(_dead_connection()), second])
    retry.assert_not_called()
    assert isinstance(exc, OperationalError)
    assert version.processing_status == ProcessingStatus.failed
    second.commit.assert_called_once()


def test_database_still_down_on_last_attempt_keeps_original_error():
    retry, exc = _run(_task().max_retries, [_session(_dead_connection()), _session(_dead_connection())])
    retry.assert_not_called()
    assert isinstance(exc, OperationalError)


def test_failure_after_setup_is_not_retried_by_the_setup_handler():
    """The transcode handler already decided; the outer handler must not retry again."""
    asset = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4(), asset_type=transcode_tasks.AssetType.video)
    version = SimpleNamespace(id=uuid.uuid4(), processing_status=ProcessingStatus.processing)
    db = _session(version, asset, SimpleNamespace(version_id=version.id, s3_key_raw="raw/x"))
    task = _task()
    task.push_request(retries=task.max_retries, called_directly=False)
    try:
        with patch.object(transcode_tasks, "SessionLocal", return_value=db), \
             patch.object(transcode_tasks, "get_s3_client", return_value=MagicMock()), \
             patch.object(transcode_tasks, "_publish_event"), \
             patch.object(transcode_tasks, "_process_video", side_effect=_dead_connection()), \
             patch.object(task, "retry") as retry, \
             pytest.raises(OperationalError):
            task.run(str(asset.id), str(version.id))
    finally:
        task.pop_request()
    retry.assert_not_called()
    assert version.processing_status == ProcessingStatus.failed
