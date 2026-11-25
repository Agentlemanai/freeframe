"""The fork's tables must never stand in the way of upstream's retention purge,
which hard-deletes versions, comments, assets and projects without knowing
these tables exist. Their foreign keys carry the delete rule instead."""
import pytest
from sqlalchemy import text

from apps.api.ext.models import FixCheck, ReviewTask, ReviewTaskNote, VersionExt, VersionResolvedTask


@pytest.fixture
def rows(real_db):
    from apps.api.models.comment import Comment
    from apps.api.tests.conftest import MediaSeed

    seed = MediaSeed(real_db)
    owner = seed.user()
    project = seed.project(owner)
    asset = seed.asset(project, owner)
    v1, _ = seed.version(asset, owner, number=1)
    v2, _ = seed.version(asset, owner, number=2)
    comment = Comment(asset_id=asset.id, version_id=v1.id, author_id=owner.id, body="c")
    real_db.add(comment)
    real_db.flush()
    task = ReviewTask(
        project_id=project.id, asset_id=asset.id, created_on_version_id=v1.id, kind="music",
        instruction="keep me", stage="rough", assignee_type="agent", created_by=owner.id,
        source_comment_id=comment.id, result_version_id=v2.id,
    )
    real_db.add(task)
    real_db.flush()
    real_db.add(FixCheck(version_id=v2.id, comment_id=comment.id, status="not_fixed"))
    real_db.flush()
    real_db.add_all([
        ReviewTaskNote(task_id=task.id, kind="agent_note", body="n"),
        VersionExt(version_id=v2.id, author_type="agent"),
        VersionResolvedTask(version_id=v2.id, task_id=task.id),
    ])
    real_db.flush()
    # Ids, not rows: the rows are about to be deleted underneath the session.
    return real_db, project.id, asset.id, v1.id, v2.id, comment.id, task.id


def _hard_delete(db, table, row_id):
    """What the purge does: a plain DELETE, no ORM cascade."""
    db.expire_all()
    for child, column in (("media_files", "version_id"),):
        if table == "asset_versions":
            db.execute(text(f"DELETE FROM {child} WHERE {column} = :id"), {"id": row_id})
    db.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})
    db.expire_all()


def test_purging_a_comment_keeps_the_task_it_became(rows):
    db, _, _, _, _, comment, task = rows
    _hard_delete(db, "comments", comment)
    kept = db.get(ReviewTask, task)
    assert kept is not None and kept.source_comment_id is None and kept.instruction == "keep me"


def test_purging_a_version_keeps_its_tasks_and_drops_its_metadata(rows):
    db, _, _, v1, v2, comment, task = rows
    _hard_delete(db, "asset_versions", v2)
    kept = db.get(ReviewTask, task)
    assert kept.result_version_id is None and kept.created_on_version_id == v1
    assert db.get(VersionExt, v2) is None
    assert db.query(VersionResolvedTask).filter(VersionResolvedTask.task_id == task).count() == 0
    _hard_delete(db, "comments", comment)
    _hard_delete(db, "asset_versions", v1)
    assert db.get(ReviewTask, task).created_on_version_id is None


def test_purging_an_asset_takes_its_tasks_and_their_notes(rows):
    db, _, asset, v1, v2, comment, task_id = rows
    _hard_delete(db, "comments", comment)
    for version in (v1, v2):
        _hard_delete(db, "asset_versions", version)
    _hard_delete(db, "assets", asset)
    assert db.get(ReviewTask, task_id) is None
    assert db.query(ReviewTaskNote).filter(ReviewTaskNote.task_id == task_id).count() == 0


def test_purging_a_comment_or_a_version_takes_its_fix_checks(rows):
    db, _, _, v1, v2, comment, _ = rows
    assert db.query(FixCheck).count() == 1
    _hard_delete(db, "asset_versions", v2)
    assert db.query(FixCheck).count() == 0
    db.add(FixCheck(version_id=v1, comment_id=comment, status="fixed"))
    db.flush()
    _hard_delete(db, "comments", comment)
    assert db.query(FixCheck).count() == 0


def test_replies_under_internal_comments_are_marked_internal_by_the_migration(real_db):
    """What the upgrade does to existing comments: a reply stored public under
    an internal comment becomes internal, however deep, and nothing else moves."""
    import importlib.util
    import pathlib

    from apps.api.models.comment import Comment
    from apps.api.tests.conftest import MediaSeed

    path = (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "e1x7a2b3c4d5_ext_review_tasks_phase1.py")
    spec = importlib.util.spec_from_file_location("ext_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    seed = MediaSeed(real_db)
    owner = seed.user()
    asset = seed.asset(seed.project(owner), owner)
    version, _ = seed.version(asset, owner)

    def comment(body, visibility, parent=None):
        c = Comment(asset_id=asset.id, version_id=version.id, author_id=owner.id, body=body,
                    visibility=visibility, parent_id=parent.id if parent else None)
        real_db.add(c)
        real_db.flush()
        return c

    internal = comment("internal", "internal")
    reply = comment("reply", "public", internal)
    deep = comment("reply to the reply", "public", reply)
    public = comment("public", "public")
    public_reply = comment("public reply", "public", public)

    real_db.execute(text(migration.MARK_REPLIES_UNDER_INTERNAL_COMMENTS))
    real_db.expire_all()
    assert [c.visibility for c in (internal, reply, deep, public, public_reply)] == [
        "internal", "internal", "internal", "public", "public",
    ]
