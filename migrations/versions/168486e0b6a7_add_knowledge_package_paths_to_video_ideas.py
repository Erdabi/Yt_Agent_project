"""add knowledge package paths to video_ideas

Revision ID: 168486e0b6a7
Revises: a032739e93ff
Create Date: 2026-07-25 22:15:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '168486e0b6a7'
down_revision: Union[str, None] = 'a032739e93ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("video_ideas", sa.Column("knowledge_package_json_path", sa.Text(), nullable=True))
    op.add_column("video_ideas", sa.Column("knowledge_package_md_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_ideas", "knowledge_package_md_path")
    op.drop_column("video_ideas", "knowledge_package_json_path")
