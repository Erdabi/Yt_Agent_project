"""consolidate video creation stage

Revision ID: a67f3ded2262
Revises: 44d6f77375f6
Create Date: 2026-07-25 21:05:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a67f3ded2262'
down_revision: Union[str, None] = '44d6f77375f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Storyboard, voiceover, video assembly, and thumbnail generation are
    # no longer separate stages the Manager Agent tracks — they became
    # internal modules of a single Video Agent
    # (services/agent_video/app/video_agent.py), run inside one job. This
    # adds the new consolidated stage value the Manager's workflow plan
    # (services/orchestrator/app/manager/workflow.py) now uses in their
    # place.
    #
    # As with the ANALYTICS value added in 44d6f77375f6: Alembic
    # autogenerate does not detect new values on an existing native
    # Postgres ENUM type, so this is hand-written. Native enum values are
    # stored by Python enum *member name*, not `.value` — this label is
    # upper-case to match every existing one.
    op.execute("ALTER TYPE project_stage ADD VALUE IF NOT EXISTS 'VIDEO_CREATION'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for enum types (see 44d6f77375f6's
    # downgrade for the same note) — not implemented, additive-only.
    #
    # Note this migration does NOT remove the now-unused STORYBOARD,
    # VOICEOVER, VIDEO_ASSEMBLY, THUMBNAIL, or ANALYTICS values that
    # earlier migrations added to this same native enum type. Postgres has
    # no way to drop a value from an existing enum type without rebuilding
    # it (create a new type, cast every column over, drop the old type) —
    # safe only once no row anywhere references the removed labels. Those
    # values remain physically defined but unreachable from application
    # code now that libs.models.enums.ProjectStage no longer declares them;
    # this is an accepted, permanent artifact of Postgres's additive-only
    # native enums, not a bug.
    pass
