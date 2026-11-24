"""ext: live gateway tokens may put people on projects (members:write)

HQ's project team becomes the source of who is on a FreeFrame project; the
gateway token that already reads and writes review tasks gets the one new
scope. Owners and admins stay out of its reach (apps/api/ext/router.py).
Revoked tokens are left alone. Downgrade takes the scope away again.

Revision ID: e2m8b4c6d8f0
Revises: e1x7a2b3c4d5
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e2m8b4c6d8f0"
down_revision: Union[str, Sequence[str], None] = "e1x7a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        update gateway_tokens set scopes = scopes || '["members:write"]'::jsonb
        where revoked_at is null and not scopes ? 'members:write'
    """)


def downgrade() -> None:
    op.execute("""
        update gateway_tokens set scopes = scopes - 'members:write'
        where scopes ? 'members:write'
    """)
