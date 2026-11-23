"""Client / internal visibility, enforced in the database queries.

A comment or a version is either `public` (the client may see it) or
`internal` (the team only). Who is "the team" is decided per project:

    internal tier   project owner or editor, or an instance admin
    client tier     everyone else who can reach the project: reviewer, viewer,
                    a direct share, a public project's visitors, a share link,
                    a guest

The rule is not applied by each endpoint. Every request's database session
carries a *viewer* (`Session.info`), and one `do_orm_execute` listener adds the
visibility condition to every ORM SELECT that touches `Comment` or
`AssetVersion`. An endpoint that forgets to filter still cannot return an
internal row to a client, which is the point: upstream has some sixty queries
on these two tables and gains more with every release.

The viewer of a session:

    (unset)        no filter. Celery tasks, scripts, the event stream's own
                   short-lived sessions. Never a request session.
    PUBLIC_ONLY    only `public` rows. Every request session starts here
                   (`database.get_db`), and a share link puts it back
                   (`permissions.validate_share_link`), so an owner looking at
                   their own share link sees what the client sees.
    a user         `public` rows, plus `internal` rows of projects where the
                   user is on the internal tier. Set when the request
                   authenticates (`middleware.auth`).
    SEE_ALL        no filter. An instance admin, or the gateway token.
"""
import uuid
from typing import Optional

from sqlalchemy import event, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from ..models.asset import Asset, AssetVersion
from ..models.comment import Comment
from ..models.project import ProjectMember, ProjectRole

PUBLIC = "public"
INTERNAL = "internal"
VISIBILITIES = (PUBLIC, INTERNAL)

#: Project roles that see internal comments and versions.
INTERNAL_ROLES = (ProjectRole.owner, ProjectRole.editor)
#: Project roles that may show something to the client: publish a version, make
#: a comment client-visible, approve or reject a task.
MANAGER_ROLES = (ProjectRole.owner,)

_VIEWER_KEY = "ext_viewer"
#: Execution option for the few queries that must count every row whoever
#: asks: `query.execution_options(**{UNFILTERED: True})`. Bookkeeping only
#: (the next version number); never for a query whose rows are returned.
UNFILTERED = "ext_unfiltered"
_SEE_ALL = "all"
_PUBLIC_ONLY = "public"


# ── Binding a viewer to a session ──────────────────────────────────────────────

def _info(db) -> Optional[dict]:
    """`Session.info`, or None for the unit tests' MagicMock session."""
    info = getattr(db, "info", None)
    return info if isinstance(info, dict) else None


def bind_public(db) -> None:
    """Only `public` rows from here on. Idempotent, and it wins over a user
    already bound: a share link is a client's view whoever opens it."""
    info = _info(db)
    if info is not None:
        info[_VIEWER_KEY] = _PUBLIC_ONLY


def bind_user(db, user) -> None:
    """Rows this user's project roles allow. An instance admin sees everything."""
    info = _info(db)
    if info is None or user is None:
        return
    if getattr(user, "is_superadmin", False) is True:
        info[_VIEWER_KEY] = _SEE_ALL
    else:
        info[_VIEWER_KEY] = user.id


def bind_all(db) -> None:
    """No filter. For the gateway token, which reads internal data by design."""
    info = _info(db)
    if info is not None:
        info[_VIEWER_KEY] = _SEE_ALL


def unbind(db) -> None:
    """Back to no filter. For a test that calls a route on its own session and
    then reads that session to check what was stored; a request's session is
    never reused, so nothing in the app calls this."""
    info = _info(db)
    if info is not None:
        info.pop(_VIEWER_KEY, None)


# ── The filter ─────────────────────────────────────────────────────────────────

def _internal_assets(user_id: uuid.UUID):
    """Ids of the assets in projects where `user_id` is on the internal tier."""
    return (
        select(Asset.id)
        .join(ProjectMember, ProjectMember.project_id == Asset.project_id)
        .where(
            ProjectMember.user_id == user_id,
            ProjectMember.deleted_at.is_(None),
            ProjectMember.role.in_(INTERNAL_ROLES),
        )
    )


def _criteria(viewer):
    if viewer == _PUBLIC_ONLY:
        return (
            Comment.visibility != INTERNAL,
            AssetVersion.visibility != INTERNAL,
        )
    assets = _internal_assets(viewer)
    return (
        or_(Comment.visibility != INTERNAL, Comment.asset_id.in_(assets)),
        or_(AssetVersion.visibility != INTERNAL, AssetVersion.asset_id.in_(assets)),
    )


@event.listens_for(Session, "do_orm_execute")
def _apply_visibility(state) -> None:
    # A refresh of an object the session already holds is a column load. It must
    # not be filtered: the author of a new internal comment is handed their own
    # row back even on a session that could not have selected it.
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get(UNFILTERED):
        return
    viewer = state.session.info.get(_VIEWER_KEY)
    if viewer is None or viewer == _SEE_ALL:
        return
    comment_rule, version_rule = _criteria(viewer)
    state.statement = state.statement.options(
        with_loader_criteria(Comment, comment_rule, include_aliases=True),
        with_loader_criteria(AssetVersion, version_rule, include_aliases=True),
    )


# ── Questions the endpoints ask ────────────────────────────────────────────────

def _role(db, project_id: uuid.UUID, user) -> Optional[ProjectRole]:
    if user is None:
        return None
    row = db.query(ProjectMember.role).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user.id,
        ProjectMember.deleted_at.is_(None),
    ).first()
    return row[0] if row else None


def is_admin(user) -> bool:
    return getattr(user, "is_superadmin", False) is True


def sees_internal(db, project_id: uuid.UUID, user) -> bool:
    """Whether `user` is on the project's internal tier."""
    if user is None:
        return False
    return is_admin(user) or _role(db, project_id, user) in INTERNAL_ROLES


def is_manager(db, project_id: uuid.UUID, user) -> bool:
    """Whether `user` may publish to the client and approve work on the project."""
    if user is None:
        return False
    return is_admin(user) or _role(db, project_id, user) in MANAGER_ROLES


def comment_visibility_for(
    db, project_id: uuid.UUID, user, requested: Optional[str], parent: Optional[Comment] = None,
) -> str:
    """The visibility a new comment by `user` is stored with.

    - A reply under an internal comment is internal, whoever writes it.
    - The client tier always writes public. It cannot ask for internal: the
      request is refused upstream of here for a share link, and downgraded to
      public would be the unsafe direction, so it is refused here too.
    - An editor always writes internal. A request for public is stored as
      internal rather than refused, so a client that predates this rule (the
      composer sends "public" unless toggled) keeps working, in the safe
      direction.
    - A manager gets what they ask for, and internal when they do not say.
    """
    from fastapi import HTTPException

    if parent is not None and parent.visibility == INTERNAL:
        return INTERNAL
    if not sees_internal(db, project_id, user):
        if requested == INTERNAL:
            raise HTTPException(status_code=403, detail="Internal comments are for the project team")
        return PUBLIC
    if not is_manager(db, project_id, user):
        return INTERNAL
    return requested if requested in VISIBILITIES else INTERNAL


def version_visibility_for(db, project_id: uuid.UUID, user) -> str:
    """The visibility a version uploaded by `user` starts with.

    A client's upload is theirs to see. A team upload is internal until a
    manager publishes it, except a manager's own, which is published already:
    they are the one who would press the button.
    """
    if sees_internal(db, project_id, user) and not is_manager(db, project_id, user):
        return INTERNAL
    return PUBLIC


def may_see_comment(db, project_id: uuid.UUID, comment, user_or_id) -> bool:
    """Whether a notification or an email about `comment` may go to this user.

    Access to the asset is the caller's question; this one is only about the
    internal tier, so that a client is never told an internal comment exists.
    """
    if comment.visibility != INTERNAL:
        return True
    user = user_or_id
    if isinstance(user_or_id, uuid.UUID):
        from ..models.user import User
        user = db.query(User).filter(User.id == user_or_id).first()
    return sees_internal(db, project_id, user)


def share_comment_visibility(db, project_id: uuid.UUID, user, requested: str) -> str:
    """Visibility of a comment a signed-in user posts through a share link.

    The share route has already refused `internal` from anyone outside the
    internal tier. What is left to decide is the team's own comments, which
    follow the same rule as in the review screen.
    """
    if user is not None and sees_internal(db, project_id, user):
        return comment_visibility_for(db, project_id, user, requested)
    return requested
