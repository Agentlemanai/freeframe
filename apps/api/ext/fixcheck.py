"""Fix check: before a new version goes to the client, the team goes through the
comments made on earlier versions and marks each one fixed, not fixed, or
won't fix. Internal only; a client never sees any of it.

Which comments are on a version's list ("to check"):
  - top-level comments (not replies) on an earlier version of the same asset,
    client-visible or internal, not deleted, not resolved;
  - minus those already marked fixed or won't fix on an earlier version, so a
    comment found not fixed on v2 comes up again on v3, and one fixed on v2
    does not.

Publishing a version (router.publish_version) waits until every comment on its
list has been checked, unless the owner publishes anyway. On publish, comments
marked fixed on this version or an earlier one are resolved, so the client sees
them as done together with the new version, not before. A won't-fix internal
comment is resolved too; a won't-fix client comment stays open for a reply.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session, aliased

from ..models.asset import AssetVersion
from ..models.comment import Annotation, Comment
from ..models.user import User
from .models import FixCheck, FixCheckStatus, ReviewTask

DECIDED_BEFORE = (FixCheckStatus.fixed.value, FixCheckStatus.wont_fix.value)


def _earlier_versions(db: Session, version: AssetVersion):
    return select(AssetVersion.id).where(
        AssetVersion.asset_id == version.asset_id,
        AssetVersion.deleted_at.is_(None),
        AssetVersion.version_number < version.version_number,
    )


def candidates(db: Session, version: AssetVersion) -> list[Comment]:
    """The comments to check on `version`, oldest version first, then by time in the video."""
    earlier = _earlier_versions(db, version)
    prior_check = aliased(FixCheck)
    prior_version = aliased(AssetVersion)
    settled_before = exists().where(
        prior_check.comment_id == Comment.id,
        prior_check.status.in_(DECIDED_BEFORE),
        prior_check.version_id == prior_version.id,
        prior_version.asset_id == version.asset_id,
        prior_version.version_number < version.version_number,
    )
    rows = (
        db.query(Comment, AssetVersion.version_number)
        .join(AssetVersion, AssetVersion.id == Comment.version_id)
        .filter(
            Comment.asset_id == version.asset_id,
            Comment.version_id.in_(earlier),
            Comment.parent_id.is_(None),
            Comment.deleted_at.is_(None),
            Comment.resolved.is_(False),
            ~settled_before,
        )
        .order_by(AssetVersion.version_number, Comment.timecode_start.asc().nulls_last(), Comment.created_at)
        .all()
    )
    return [c for c, _ in rows]


def checklist(db: Session, version: AssetVersion) -> dict:
    comments = candidates(db, version)
    ids = [c.id for c in comments]
    checks = {
        ch.comment_id: ch for ch in db.query(FixCheck).filter(FixCheck.version_id == version.id).all()
    }
    numbers = dict(db.query(AssetVersion.id, AssetVersion.version_number).filter(
        AssetVersion.asset_id == version.asset_id).all())
    people = {
        u.id: u.name for u in db.query(User).filter(User.id.in_(
            {c.author_id for c in comments if c.author_id} | {ch.checked_by for ch in checks.values() if ch.checked_by}
        )).all()
    } if comments or checks else {}
    drawings = {}
    if ids:
        for a in db.query(Annotation).filter(Annotation.comment_id.in_(ids)).all():
            drawings.setdefault(a.comment_id, a.drawing_data)
    tasks = {}
    if ids:
        for t in db.query(ReviewTask).filter(ReviewTask.source_comment_id.in_(ids)).order_by(ReviewTask.created_at).all():
            tasks[t.source_comment_id] = t  # the newest task made from the comment
    # where a comment was last found not fixed
    last_not_fixed = {}
    if ids:
        for comment_id, number in (
            db.query(FixCheck.comment_id, AssetVersion.version_number)
            .join(AssetVersion, AssetVersion.id == FixCheck.version_id)
            .filter(FixCheck.comment_id.in_(ids), FixCheck.status == FixCheckStatus.not_fixed.value,
                    AssetVersion.version_number < version.version_number)
            .all()
        ):
            last_not_fixed[comment_id] = max(number, last_not_fixed.get(comment_id, 0))

    items = []
    for c in comments:
        ch = checks.get(c.id)
        t = tasks.get(c.id)
        items.append({
            "comment_id": c.id,
            "version_id": c.version_id,
            "version_number": numbers.get(c.version_id),
            "timecode_start": c.timecode_start,
            "timecode_end": c.timecode_end,
            "body": c.body,
            "author_name": people.get(c.author_id) if c.author_id else "Guest",
            "visibility": c.visibility,
            "drawing": drawings.get(c.id),
            "task_id": t.id if t else None,
            "task_status": t.status if t else None,
            "not_fixed_in": last_not_fixed.get(c.id),
            "status": ch.status if ch else None,
            "note": ch.note if ch else None,
            "checked_by_name": people.get(ch.checked_by) if ch and ch.checked_by else None,
            "checked_at": ch.checked_at if ch else None,
        })
    previous = (
        db.query(AssetVersion.id)
        .filter(AssetVersion.asset_id == version.asset_id, AssetVersion.deleted_at.is_(None),
                AssetVersion.version_number < version.version_number)
        .order_by(AssetVersion.version_number.desc()).limit(1).scalar()
    )
    return {
        "version_id": version.id,
        "version_number": version.version_number,
        "previous_version_id": previous,
        "total": len(items),
        "checked": sum(1 for i in items if i["status"]),
        "items": items,
    }


def unchecked_count(db: Session, version: AssetVersion) -> int:
    ids = [c.id for c in candidates(db, version)]
    if not ids:
        return 0
    done = db.query(FixCheck.comment_id).filter(FixCheck.version_id == version.id, FixCheck.comment_id.in_(ids)).count()
    return len(ids) - done


def set_check(db: Session, version: AssetVersion, comment_id: uuid.UUID, status: Optional[str],
              note: Optional[str], user_id: uuid.UUID) -> Optional[FixCheck]:
    """Record (or with status None, clear) one check. The comment must be on the version's list."""
    if comment_id not in {c.id for c in candidates(db, version)}:
        raise LookupError("This comment is not on this version's list")
    row = db.query(FixCheck).filter(FixCheck.version_id == version.id, FixCheck.comment_id == comment_id).first()
    if status is None:
        if row is not None:
            db.delete(row)
        db.flush()
        return None
    if row is None:
        row = FixCheck(version_id=version.id, comment_id=comment_id, status=status)
        db.add(row)
    row.status = status
    row.note = (note or "").strip() or None
    row.checked_by = user_id
    row.checked_at = datetime.now(timezone.utc)
    db.flush()
    return row


def resolve_on_publish(db: Session, version: AssetVersion) -> list[uuid.UUID]:
    """Resolve the comments the team found fixed on this version or an earlier one,
    and internal comments it won't fix. Returns the resolved comment ids."""
    checked_upto = (
        select(FixCheck.comment_id, FixCheck.status, Comment.visibility)
        .join(AssetVersion, AssetVersion.id == FixCheck.version_id)
        .join(Comment, Comment.id == FixCheck.comment_id)
        .where(
            AssetVersion.asset_id == version.asset_id,
            AssetVersion.version_number <= version.version_number,
            Comment.resolved.is_(False),
            Comment.deleted_at.is_(None),
        )
    )
    ids = sorted({
        cid for cid, status, vis in db.execute(checked_upto).all()
        if status == FixCheckStatus.fixed.value or (status == FixCheckStatus.wont_fix.value and vis == "internal")
    })
    if ids:
        db.query(Comment).filter(Comment.id.in_(ids)).update({Comment.resolved: True}, synchronize_session=False)
    return ids
