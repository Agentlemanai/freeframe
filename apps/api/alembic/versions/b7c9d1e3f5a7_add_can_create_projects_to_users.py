"""add can_create_projects to users

Revision ID: b7c9d1e3f5a7
Revises: a9e1c3b5d7f2
Create Date: 2026-10-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b7c9d1e3f5a7'
down_revision: Union[str, Sequence[str], None] = 'a9e1c3b5d7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing users keep the behaviour they had: everyone could create projects.
    op.add_column('users', sa.Column('can_create_projects', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('users', 'can_create_projects')
