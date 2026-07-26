"""add script structure and visual fields

Revision ID: 3b66fa18972b
Revises: cb4816693ad3
Create Date: 2026-07-26 17:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3b66fa18972b'
down_revision: Union[str, None] = 'cb4816693ad3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# A brand-new type with no historical column data yet (the Script Agent
# was a NotImplementedError stub until now) — fully reversible, same
# situation as `competition_level` in a032739e93ff.
_script_segment_type = postgresql.ENUM(
    "HOOK", "INTRODUCTION", "MAIN_SECTION", "ENDING", "CALL_TO_ACTION",
    name="script_segment_type",
)


def upgrade() -> None:
    op.add_column("scripts", sa.Column("structure_notes", sa.Text(), nullable=True))
    op.add_column("scripts", sa.Column("retention_notes", sa.Text(), nullable=True))

    _script_segment_type.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "script_segments",
        sa.Column(
            "segment_type",
            postgresql.ENUM(
                "HOOK", "INTRODUCTION", "MAIN_SECTION", "ENDING", "CALL_TO_ACTION",
                name="script_segment_type", create_type=False,
            ),
            # No historical rows exist for this table yet, so NOT NULL is
            # safe to add directly — nothing to backfill.
            nullable=False,
        ),
    )
    op.add_column("script_segments", sa.Column("visual_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("script_segments", "visual_notes")
    op.drop_column("script_segments", "segment_type")
    _script_segment_type.drop(op.get_bind(), checkfirst=True)

    op.drop_column("scripts", "retention_notes")
    op.drop_column("scripts", "structure_notes")
