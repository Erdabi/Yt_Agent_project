"""add llm usage log

Revision ID: cb4816693ad3
Revises: 168486e0b6a7
Create Date: 2026-07-26 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cb4816693ad3'
down_revision: Union[str, None] = '168486e0b6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('llm_usage_log',
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('agent_name', sa.String(length=100), nullable=False),
    sa.Column('call_site', sa.String(length=200), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('prompt_name', sa.String(length=200), nullable=True),
    sa.Column('prompt_version', sa.String(length=50), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('elapsed_ms', sa.Integer(), nullable=False),
    sa.Column('cost_estimate_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('called_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_usage_log_project_id'), 'llm_usage_log', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_llm_usage_log_project_id'), table_name='llm_usage_log')
    op.drop_table('llm_usage_log')
