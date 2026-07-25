"""add analytics project stage

Revision ID: 44d6f77375f6
Revises: 70d0072ec4f2
Create Date: 2026-07-25 20:28:04.454798

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '44d6f77375f6'
down_revision: Union[str, None] = '70d0072ec4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alembic's autogenerate does not detect new values added to an
    # existing native Postgres ENUM type, so this is hand-written rather
    # than generated. Adds the final stage the Manager Agent's fixed
    # workflow plan (services/orchestrator/app/manager/workflow.py) moves
    # a project into after publishing succeeds.
    # Native enum values are stored by Python enum *member name*
    # (SQLAlchemy's default), not by `.value` — every existing label here
    # is upper-case to match, so this one must be too.
    op.execute("ALTER TYPE project_stage ADD VALUE IF NOT EXISTS 'ANALYTICS'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for enum types — removing one requires
    # rebuilding the type (create a new type without it, cast the column
    # over, drop the old type), which is only safe if no row currently
    # uses 'analytics'. Not implemented: this migration is additive-only,
    # consistent with how the rest of this schema treats enum growth.
    pass
