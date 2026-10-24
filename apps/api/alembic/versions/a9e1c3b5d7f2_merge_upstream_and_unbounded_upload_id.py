"""merge upstream head with the unbounded upload_id fork migration

The fork's f1b3d5a7c9e2 (upload_id -> TEXT, for Cloudflare R2's 343-character
multipart IDs) and upstream's chain ending in c5d1f3a7b9e2 both branch from
e3f5a7c9d1b2. `alembic upgrade head` refuses multiple heads, so this joins them.
No schema change of its own.

Revision ID: a9e1c3b5d7f2
Revises: c5d1f3a7b9e2, f1b3d5a7c9e2
Create Date: 2026-10-24
"""
from typing import Sequence, Union

revision: str = 'a9e1c3b5d7f2'
down_revision: Union[str, Sequence[str], None] = ('c5d1f3a7b9e2', 'f1b3d5a7c9e2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
