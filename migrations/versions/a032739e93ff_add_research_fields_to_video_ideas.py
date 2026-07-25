"""add research fields to video_ideas

Revision ID: a032739e93ff
Revises: a67f3ded2262
Create Date: 2026-07-25 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a032739e93ff'
down_revision: Union[str, None] = 'a67f3ded2262'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Unlike the ProjectStage migrations before this one, `competition_level`
# is a brand-new type with no historical column data yet — fully
# reversible, so (unlike those) this downgrade actually drops it.
_competition_level = postgresql.ENUM("LOW", "MEDIUM", "HIGH", name="competition_level")


def upgrade() -> None:
    _competition_level.create(op.get_bind(), checkfirst=True)
    op.add_column("video_ideas", sa.Column("target_audience", sa.Text(), nullable=True))
    op.add_column("video_ideas", sa.Column("suggested_angle", sa.Text(), nullable=True))
    op.add_column(
        "video_ideas",
        sa.Column(
            "competition_level",
            postgresql.ENUM("LOW", "MEDIUM", "HIGH", name="competition_level", create_type=False),
            nullable=True,
        ),
    )
    op.add_column("video_ideas", sa.Column("suggested_length_sec", sa.Integer(), nullable=True))
    op.add_column("video_ideas", sa.Column("research_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_ideas", "research_notes")
    op.drop_column("video_ideas", "suggested_length_sec")
    op.drop_column("video_ideas", "competition_level")
    op.drop_column("video_ideas", "suggested_angle")
    op.drop_column("video_ideas", "target_audience")
    _competition_level.drop(op.get_bind(), checkfirst=True)
