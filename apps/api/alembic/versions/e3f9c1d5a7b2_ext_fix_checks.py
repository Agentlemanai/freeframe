"""ext: fix checks — earlier comments checked on a new version before it goes to the client

One row per (new version, earlier comment): fixed, not fixed or won't fix, with a
note, who and when. Internal only. Cascades with the version and the comment, so
upstream's retention purge needs no knowledge of it. No row = not checked yet.

Revision ID: e3f9c1d5a7b2
Revises: e2m8b4c6d8f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3f9c1d5a7b2"
down_revision: Union[str, Sequence[str], None] = "e2m8b4c6d8f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ext_fix_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("asset_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("comments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("checked_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status in ('fixed', 'not_fixed', 'wont_fix')", name="ck_ext_fix_checks_status"),
    )
    op.create_index("ux_ext_fix_checks_version_comment", "ext_fix_checks", ["version_id", "comment_id"], unique=True)
    op.create_index("ix_ext_fix_checks_comment_id", "ext_fix_checks", ["comment_id"])


def downgrade() -> None:
    op.drop_index("ix_ext_fix_checks_comment_id", table_name="ext_fix_checks")
    op.drop_index("ux_ext_fix_checks_version_comment", table_name="ext_fix_checks")
    op.drop_table("ext_fix_checks")
