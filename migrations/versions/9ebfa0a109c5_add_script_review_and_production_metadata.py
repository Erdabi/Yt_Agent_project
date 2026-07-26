"""add script review notes and segment production metadata

Revision ID: 9ebfa0a109c5
Revises: 3b66fa18972b
Create Date: 2026-07-26 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9ebfa0a109c5'
down_revision: Union[str, None] = '3b66fa18972b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scripts", sa.Column("review_notes", sa.Text(), nullable=True))
    op.add_column(
        "script_segments",
        sa.Column(
            "production_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default='{}',
        ),
    )


def downgrade() -> None:
    op.drop_column("script_segments", "production_metadata")
    op.drop_column("scripts", "review_notes")
