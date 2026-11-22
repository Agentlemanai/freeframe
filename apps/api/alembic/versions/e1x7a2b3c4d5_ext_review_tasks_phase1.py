"""ext: review tasks, version visibility, event outbox, gateway tokens

The fork's review-driven editing, phase 1 (apps/api/ext). Adds one column to an
upstream table and six tables of its own.

`asset_versions.visibility` gets the server default 'public', so every version
that exists when this runs stays visible to the people who can see it today.
`comments.visibility` is upstream's column (4094df400c86) and is not touched:
comments already marked internal stay internal.

Revision ID: e1x7a2b3c4d5
Revises: d4f6b8a0c2e1
Create Date: 2026-11-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'e1x7a2b3c4d5'
down_revision: Union[str, Sequence[str], None] = 'd4f6b8a0c2e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_uuid = postgresql.UUID(as_uuid=True)
_now = sa.text('now()')

# A name of its own so the test can run exactly this statement.
MARK_REPLIES_UNDER_INTERNAL_COMMENTS = """
    WITH RECURSIVE under_internal AS (
        SELECT id FROM comments WHERE visibility = 'internal'
        UNION
        SELECT c.id FROM comments c JOIN under_internal u ON c.parent_id = u.id
    )
    UPDATE comments SET visibility = 'internal'
    WHERE id IN (SELECT id FROM under_internal) AND visibility <> 'internal'
"""


def upgrade() -> None:
    op.add_column(
        'asset_versions',
        sa.Column('visibility', sa.String(20), nullable=False, server_default='public'),
    )

    # A reply under an internal comment is internal. Until now a reply could be
    # stored `public` under an internal parent (the composer's default); it was
    # only ever shown inside its parent's thread, which a share link never
    # rendered. Flat reads (the export, counts) are now filtered per row, so the
    # rows have to say what they are.
    op.execute(MARK_REPLIES_UNDER_INTERNAL_COMMENTS)

    op.create_table(
        'review_tasks',
        sa.Column('id', _uuid, primary_key=True),
        sa.Column('project_id', _uuid, sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('asset_id', _uuid, sa.ForeignKey('assets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_on_version_id', _uuid, sa.ForeignKey('asset_versions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('frame_in', sa.Integer(), nullable=True),
        sa.Column('frame_out', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('instruction', sa.Text(), nullable=False),
        sa.Column('stage', sa.String(20), nullable=False),
        sa.Column('assignee_type', sa.String(20), nullable=False),
        sa.Column('assignee_person_id', _uuid, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('assignee_role', sa.String(50), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='open'),
        sa.Column('resume_status', sa.String(20), nullable=True),
        sa.Column('source_comment_id', _uuid, sa.ForeignKey('comments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('result_version_id', _uuid, sa.ForeignKey('asset_versions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('claimed_by', sa.String(255), nullable=True),
        sa.Column('created_by', _uuid, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
    )
    op.create_index('ix_review_tasks_project_status', 'review_tasks', ['project_id', 'status'])
    op.create_index('ix_review_tasks_asset_id', 'review_tasks', ['asset_id'])
    op.create_index('ix_review_tasks_assignee_person_id', 'review_tasks', ['assignee_person_id'])
    op.create_index('ix_review_tasks_source_comment_id', 'review_tasks', ['source_comment_id'])

    op.create_table(
        'review_task_notes',
        sa.Column('id', _uuid, primary_key=True),
        sa.Column('task_id', _uuid, sa.ForeignKey('review_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('author_type', sa.String(20), nullable=False),
        sa.Column('author_person_id', _uuid, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('author_label', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
    )
    op.create_index('ix_review_task_notes_task_id', 'review_task_notes', ['task_id'])

    op.create_table(
        'version_ext',
        sa.Column('version_id', _uuid, sa.ForeignKey('asset_versions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('resolve_project', sa.String(255), nullable=True),
        sa.Column('resolve_timeline', sa.String(255), nullable=True),
        sa.Column('start_timecode', sa.String(20), nullable=True),
        sa.Column('fps', sa.Float(), nullable=True),
        sa.Column('author_type', sa.String(20), nullable=False),
        sa.Column('author_person_id', _uuid, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('agent_machine', sa.String(255), nullable=True),
        sa.Column('published_by', _uuid, sa.ForeignKey('users.id'), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
    )

    op.create_table(
        'version_resolved_tasks',
        sa.Column('version_id', _uuid, sa.ForeignKey('asset_versions.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('task_id', _uuid, sa.ForeignKey('review_tasks.id', ondelete='CASCADE'), primary_key=True),
    )
    op.create_index('ix_version_resolved_tasks_task_id', 'version_resolved_tasks', ['task_id'])

    op.create_table(
        'ext_events',
        sa.Column('seq', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('project_id', _uuid, nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_ext_events_project_id', 'ext_events', ['project_id'])
    op.create_index(
        'ix_ext_events_undelivered', 'ext_events', ['seq'],
        postgresql_where=sa.text('delivered_at IS NULL'),
    )

    op.create_table(
        'gateway_tokens',
        sa.Column('id', _uuid, primary_key=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('scopes', postgresql.JSONB(), nullable=False),
        sa.Column('created_by', _uuid, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=_now, nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('gateway_tokens')
    op.drop_index('ix_ext_events_undelivered', table_name='ext_events')
    op.drop_index('ix_ext_events_project_id', table_name='ext_events')
    op.drop_table('ext_events')
    op.drop_index('ix_version_resolved_tasks_task_id', table_name='version_resolved_tasks')
    op.drop_table('version_resolved_tasks')
    op.drop_table('version_ext')
    op.drop_index('ix_review_task_notes_task_id', table_name='review_task_notes')
    op.drop_table('review_task_notes')
    for ix in ('ix_review_tasks_source_comment_id', 'ix_review_tasks_assignee_person_id',
               'ix_review_tasks_asset_id', 'ix_review_tasks_project_status'):
        op.drop_index(ix, table_name='review_tasks')
    op.drop_table('review_tasks')
    op.drop_column('asset_versions', 'visibility')
