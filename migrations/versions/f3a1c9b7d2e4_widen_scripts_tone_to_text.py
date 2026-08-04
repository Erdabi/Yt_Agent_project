"""widen scripts.tone from varchar(100) to text

Revision ID: f3a1c9b7d2e4
Revises: de480465463f
Create Date: 2026-07-30 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3a1c9b7d2e4'
down_revision: Union[str, None] = 'de480465463f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # scripts.tone is populated from project_context.channel.persona
    # (services/agent_scriptwriter/app/worker.py) — the channel's full
    # persona/style-guide text, documented as deliberately unstructured
    # and unbounded (libs/models/channel.py's Channel.persona_config).
    # The original varchar(100) cap crashes the Script Agent's DB write
    # for any channel whose persona exceeds 100 characters, which the
    # column's own producer never guaranteed.
    op.alter_column("scripts", "tone", existing_type=sa.String(length=100), type_=sa.Text(), nullable=True)


def downgrade() -> None:
    op.alter_column("scripts", "tone", existing_type=sa.Text(), type_=sa.String(length=100), nullable=True)
