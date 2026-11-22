"""Live (SSE) events that the client tier must not receive.

Upstream publishes every project event on one Redis channel, `project:<id>`.
An event about an internal comment or version goes to a second channel,
`project:<id>:internal`, which only the internal tier's streams subscribe to.
A client's stream never receives it, so there is nothing to filter out.

`event_service.publish_sync` builds the channel name from whatever it is given
as the project id, so "<id>:internal" is all the routing it needs.
"""
import uuid

from ..models.asset import AssetVersion
from . import visibility

INTERNAL_SUFFIX = ":internal"


def channel_key(project_id, *, internal: bool):
    """What to hand `publish_sync` as its project id: the id itself, untouched,
    for the project's own channel, or "<id>:internal"."""
    return f"{project_id}{INTERNAL_SUFFIX}" if internal else project_id


def comment_channel_key(project_id, comment):
    return channel_key(project_id, internal=getattr(comment, "visibility", None) == visibility.INTERNAL)


def stream_channels(project_id, *, internal: bool) -> list[str]:
    """Redis channels a subscriber's stream listens on."""
    channels = [f"project:{project_id}"]
    if internal:
        channels.append(f"project:{project_id}{INTERNAL_SUFFIX}")
    return channels


def sees_internal_events(db, project_id, user_id) -> bool:
    """Whether a stream opened by `user_id` gets the internal channel. Asked at
    connect and again on every recheck, so a demotion closes the stream."""
    from ..models.user import User
    user = db.get(User, user_id)
    return user is not None and visibility.sees_internal(db, project_id, user)


# ── Callers that have a version id but no session ──────────────────────────────

_CACHE_SECONDS = 30.0
_cache: dict[str, tuple[float, bool]] = {}


def _version_is_internal(version_id) -> bool:
    """Looked up on a session of its own, and remembered briefly: a transcode
    publishes a progress event several times a second."""
    import time
    from ..database import SessionLocal

    key = str(version_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    db = SessionLocal()
    try:
        found = db.query(AssetVersion.visibility).filter(AssetVersion.id == uuid.UUID(key)).first()
    finally:
        db.close()
    # A version with no row has nothing left to give away.
    internal = found is not None and found[0] != visibility.PUBLIC
    if len(_cache) > 512:
        _cache.clear()
    _cache[key] = (now, internal)
    return internal


def forget_version(version_id) -> None:
    """Drop the remembered answer, when a version's visibility changes."""
    _cache.pop(str(version_id), None)


def transcode_channel_key(project_id, version_id):
    """The channel for an event about `version_id`, for a publisher with no
    session at hand (the transcoder, the verdict service).

    Never raises: a live update is not worth failing a transcode over. If the
    lookup fails the event goes to the internal channel, the safe side.
    """
    if not version_id:
        return project_id
    try:
        return channel_key(project_id, internal=_version_is_internal(version_id))
    except Exception:
        return channel_key(project_id, internal=True)
