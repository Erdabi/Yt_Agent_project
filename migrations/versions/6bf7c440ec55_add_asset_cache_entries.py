"""add asset_cache_entries

Revision ID: 6bf7c440ec55
Revises: eadda3f468d5
Create Date: 2026-07-26 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6bf7c440ec55'
down_revision: Union[str, None] = 'eadda3f468d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'asset_cache_entries',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('cache_key', sa.String(length=64), nullable=False),
        sa.Column('capability', sa.String(length=50), nullable=False),
        sa.Column('provider_name', sa.String(length=100), nullable=False),
        sa.Column('asset_type', sa.String(length=50), nullable=False),
        # Reuses the `asset_type` native enum already created for
        # `assets.type` (see 70d0072ec4f2's initial schema) —
        # `create_type=False` so Alembic doesn't try to `CREATE TYPE` it
        # again, same pattern as a032739e93ff/3b66fa18972b.
        sa.Column(
            'physical_asset_type',
            postgresql.ENUM(
                'AUDIO', 'IMAGE', 'VIDEO', 'MUSIC', 'CAPTION',
                name='asset_type', create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('storage_path', sa.Text(), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cache_key'),
    )


def downgrade() -> None:
    op.drop_table('asset_cache_entries')
