"""add audio_library provider capability

Revision ID: eadda3f468d5
Revises: 9ebfa0a109c5
Create Date: 2026-07-26 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'eadda3f468d5'
down_revision: Union[str, None] = '9ebfa0a109c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Alembic's autogenerate does not detect new values added to an
    # existing native Postgres ENUM type, so this is hand-written rather
    # than generated — same pattern as 44d6f77375f6/a67f3ded2262 adding
    # values to project_stage. Sourcing supplementary audio (sound
    # effects, background music cues) for the Video Agent's Asset
    # Generation module (libs/providers/audio_library/) needs its own
    # provider_capability value, distinct from `tts` (narration) and
    # `stock_media` (visual footage).
    op.execute("ALTER TYPE provider_capability ADD VALUE IF NOT EXISTS 'AUDIO_LIBRARY'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for enum types — removing one requires
    # rebuilding the type, which is only safe if no row currently uses
    # 'audio_library'. Not implemented: this migration is additive-only,
    # consistent with how the rest of this schema treats enum growth.
    pass
