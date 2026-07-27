"""add channel id, url, uploaded_at, playlist_id to publications

Revision ID: de480465463f
Revises: 50a8aa4a70f4
Create Date: 2026-07-27 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'de480465463f'
down_revision: Union[str, None] = '50a8aa4a70f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Four of the eight fields the newly-implemented Publisher Agent
    # (services/agent_publisher/app/publisher_agent.py) is required to
    # persist per upload were still missing from this table — see
    # libs/providers/youtube/base.py's `UploadResult` for where each one
    # comes from.
    op.add_column("publications", sa.Column("youtube_channel_id", sa.String(length=64), nullable=True))
    op.add_column("publications", sa.Column("url", sa.String(length=500), nullable=True))
    op.add_column("publications", sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("publications", sa.Column("playlist_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("publications", "playlist_id")
    op.drop_column("publications", "uploaded_at")
    op.drop_column("publications", "url")
    op.drop_column("publications", "youtube_channel_id")
