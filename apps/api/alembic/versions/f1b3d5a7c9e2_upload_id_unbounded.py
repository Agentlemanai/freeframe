"""store multipart upload IDs of any length

initiate records the storage backend's multipart UploadId on the version, in a
VARCHAR(255). S3 and MinIO hand out IDs comfortably under that, but Cloudflare
R2's are longer -- 343 characters for this app's key layout -- so on R2 the
UPDATE fails, initiate answers 500, and no upload can start at all.

The ID is opaque and its length is the backend's business, so the column becomes
TEXT rather than a larger VARCHAR that the next backend could outgrow.

Revision ID: f1b3d5a7c9e2
Revises: e3f5a7c9d1b2
Create Date: 2026-09-19
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f1b3d5a7c9e2'
down_revision: Union[str, Sequence[str], None] = 'e3f5a7c9d1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # VARCHAR(n) -> TEXT is binary-compatible in Postgres: a catalogue change,
    # no table rewrite, existing values untouched.
    op.alter_column(
        'asset_versions', 'upload_id',
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Fails rather than truncates if a stored ID is over 255 characters: a
    # cut-down UploadId is useless, so losing it silently would be worse.
    op.alter_column(
        'asset_versions', 'upload_id',
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
