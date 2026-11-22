"""merge upstream's head (review verdicts) with the fork's head (can_create_projects)

Upstream's chain c7f1a4b2e085 -> f4b9c2a7d105 -> 7c3a9d4b1e2f -> a6c3e9d1f472 ->
a7e3c5d90b42 and the fork's a9e1c3b5d7f2 -> b7c9d1e3f5a7 both descend from
c5d1f3a7b9e2. `alembic upgrade head` refuses multiple heads, so this joins them.
No schema change of its own.

Upstream's a6c3e9d1f472 widens asset_versions.upload_id to TEXT, which the fork
already did in f1b3d5a7c9e2. On a database that ran the fork's migration the
column is TEXT already and PostgreSQL treats the second ALTER as a no-op.

Revision ID: d4f6b8a0c2e1
Revises: a7e3c5d90b42, b7c9d1e3f5a7
Create Date: 2026-11-22
"""
from typing import Sequence, Union

revision: str = 'd4f6b8a0c2e1'
down_revision: Union[str, Sequence[str], None] = ('a7e3c5d90b42', 'b7c9d1e3f5a7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
