from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Channel(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The top-level entity. Supports multi-channel operation from day one
    even when only a single channel is in use.
    """

    __tablename__ = "channels"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    niche: Mapped[str | None] = mapped_column(String(255))
    # Tone, banned topics, cadence, style guide — deliberately unstructured
    # because it evolves per-channel without needing a migration each time.
    persona_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    video_ideas: Mapped[list["VideoIdea"]] = relationship(back_populates="channel")
    projects: Mapped[list["Project"]] = relationship(back_populates="channel")
    provider_configs: Mapped[list["ProviderConfig"]] = relationship(
        back_populates="channel"
    )
