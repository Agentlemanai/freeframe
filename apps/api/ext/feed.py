"""The comment feed: a video's comments with their frames, for the gateway and the
team's tools (Garlo Worker puts them on the timeline in Resolve, or saves a marker
file for Premiere or After Effects; an agent reads them over MCP).

Read only. The session's viewer decides what is in it, as everywhere else: the
gateway and the team see internal comments, nobody else gets this far
(router._require_internal).
"""
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.asset import Asset, AssetVersion, MediaFile, ProcessingStatus
from ..models.comment import Annotation, Comment
from ..models.user import GuestUser, User
from .models import VersionExt


def pick_version(db: Session, asset: Asset, version_id: Optional[uuid.UUID]) -> AssetVersion:
    q = db.query(AssetVersion).filter(AssetVersion.asset_id == asset.id, AssetVersion.deleted_at.is_(None))
    if version_id is not None:
        version = q.filter(AssetVersion.id == version_id).first()
    else:
        version = q.filter(AssetVersion.processing_status == ProcessingStatus.ready) \
            .order_by(AssetVersion.version_number.desc()).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


def version_timing(db: Session, version: AssetVersion) -> dict:
    """What a timeline needs: fps, length, and where the version came from in Resolve."""
    ext = db.get(VersionExt, version.id)
    media = db.query(MediaFile).filter(MediaFile.version_id == version.id).first()
    fps = (ext.fps if ext and ext.fps else None) or (media.fps if media and media.fps else None)
    return {
        "id": version.id,
        "version_number": version.version_number,
        "visibility": version.visibility,
        "fps": fps,
        "duration_seconds": media.duration_seconds if media else None,
        "resolve_project": ext.resolve_project if ext else None,
        "resolve_timeline": ext.resolve_timeline if ext else None,
        "start_timecode": ext.start_timecode if ext else None,
    }


def _frame(seconds: Optional[float], fps: Optional[float]) -> Optional[int]:
    if seconds is None or not fps:
        return None
    return max(0, round(seconds * fps))


def feed(db: Session, asset: Asset, version_id: Optional[uuid.UUID], include_resolved: bool) -> dict:
    version = pick_version(db, asset, version_id)
    timing = version_timing(db, version)
    fps = timing["fps"]
    rows = db.query(Comment).filter(
        Comment.asset_id == asset.id, Comment.version_id == version.id, Comment.deleted_at.is_(None),
    ).order_by(Comment.timecode_start.asc().nulls_last(), Comment.created_at).all()
    users = {u.id: u for u in db.query(User).filter(User.id.in_({c.author_id for c in rows if c.author_id})).all()} \
        if any(c.author_id for c in rows) else {}
    guests = {g.id: g for g in db.query(GuestUser).filter(GuestUser.id.in_({c.guest_author_id for c in rows if c.guest_author_id})).all()} \
        if any(c.guest_author_id for c in rows) else {}
    drawn = {a for (a,) in db.query(Annotation.comment_id).filter(Annotation.comment_id.in_([c.id for c in rows])).all()} \
        if rows else set()

    def who(c: Comment) -> str:
        p = users.get(c.author_id) or guests.get(c.guest_author_id)
        return (p.name or p.email) if p else "Unknown"

    replies: dict = {}
    for c in rows:
        if c.parent_id is not None:
            replies.setdefault(c.parent_id, []).append(c)
    comments = []
    for c in rows:
        if c.parent_id is not None or (c.resolved and not include_resolved):
            continue
        comments.append({
            "id": c.id,
            "timecode_start": c.timecode_start,
            "timecode_end": c.timecode_end,
            "frame_in": _frame(c.timecode_start, fps),
            "frame_out": _frame(c.timecode_end if c.timecode_end is not None else c.timecode_start, fps),
            "body": c.body,
            "author_name": who(c),
            "visibility": c.visibility,
            "resolved": bool(c.resolved),
            "has_drawing": c.id in drawn,
            "created_at": c.created_at,
            "replies": [
                {"author_name": who(r), "body": r.body, "visibility": r.visibility, "created_at": r.created_at}
                for r in sorted(replies.get(c.id, []), key=lambda r: r.created_at)
            ],
        })
    versions = db.query(AssetVersion).filter(
        AssetVersion.asset_id == asset.id, AssetVersion.deleted_at.is_(None),
    ).order_by(AssetVersion.version_number.desc()).all()
    return {
        "asset_id": asset.id,
        "asset_name": asset.name,
        "project_id": asset.project_id,
        "version": timing,
        "versions": [{"id": v.id, "version_number": v.version_number, "visibility": v.visibility,
                      "ready": v.processing_status == ProcessingStatus.ready} for v in versions],
        "comments": comments,
    }
