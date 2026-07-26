"""add model and language to voiceovers

Revision ID: 523cb69e24aa
Revises: 6bf7c440ec55
Create Date: 2026-07-26 23:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '523cb69e24aa'
down_revision: Union[str, None] = '6bf7c440ec55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `provider`/`voice_id` already existed but were never actually
    # populated (Voice Generation never passed a voice_id through, and
    # no provider reported one back) until the real TTS provider
    # interface started reporting model/voice_id/language explicitly
    # (libs/providers/tts/base.py's SynthesisResult) — these two columns
    # complete that same metadata bundle: provider, model, voice id,
    # language, duration all live on this one row.
    op.add_column("voiceovers", sa.Column("model", sa.String(length=100), nullable=True))
    op.add_column("voiceovers", sa.Column("language", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("voiceovers", "language")
    op.drop_column("voiceovers", "model")
