"""add metadata to qa_reports

Revision ID: 50a8aa4a70f4
Revises: 523cb69e24aa
Create Date: 2026-07-27 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '50a8aa4a70f4'
down_revision: Union[str, None] = '523cb69e24aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-reviewer review metadata (which reviewers ran, each one's own
    # passed/summary, llm provider/model) for the newly-implemented
    # Quality Control Agent (services/agent_qa/app/quality_control_agent.py)
    # — kept separate from the pre-existing `issues` column, the same
    # "thin typed columns + one JSONB metadata blob" split every other
    # table in this schema already uses (e.g. `assets.metadata`).
    op.add_column(
        "qa_reports",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )
    op.alter_column("qa_reports", "metadata", server_default=None)


def downgrade() -> None:
    op.drop_column("qa_reports", "metadata")
