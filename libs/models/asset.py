from sqlalchemy import Boolean, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import AssetType


class Asset(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Generic artifact table. Every media file the pipeline produces or
    sources — audio, image, video, music, captions — is a row here,
    pointing at object storage (MinIO/S3). The database never stores the
    media itself.
    """

    __tablename__ = "assets"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[AssetType] = mapped_column(
        Enum(AssetType, name="asset_type", native_enum=True), nullable=False
    )
    # Which provider/source produced it (e.g. "elevenlabs", "pexels");
    # null for a manually uploaded asset.
    provider: Mapped[str | None] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    # Content hash — both an integrity check and the idempotency guard that
    # lets a retried job detect it already produced this exact output.
    checksum: Mapped[str | None] = mapped_column(String(128))
    duration_sec: Mapped[float | None] = mapped_column(Numeric(8, 2))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    project: Mapped["Project"] = relationship(back_populates="assets")


class Voiceover(Base, UUIDPrimaryKeyMixin):
    """Links a script segment to the audio asset the Voice-over Agent
    produced for it.
    """

    __tablename__ = "voiceovers"

    script_segment_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("script_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(100))
    voice_id: Mapped[str | None] = mapped_column(String(100))
    # Which concrete TTS model the provider used (e.g. "eleven_multilingual_v2")
    # — distinct from `provider` (the vendor/class), since one vendor can
    # offer several models with different quality/latency/cost tradeoffs.
    model: Mapped[str | None] = mapped_column(String(100))
    language: Mapped[str | None] = mapped_column(String(20))
    duration_sec: Mapped[float | None] = mapped_column(Numeric(8, 2))

    script_segment: Mapped["ScriptSegment"] = relationship(back_populates="voiceovers")
    asset: Mapped["Asset"] = relationship()


class Render(Base, UUIDPrimaryKeyMixin):
    """The final assembled video for a project, produced by the Video
    Assembly Agent.
    """

    __tablename__ = "renders"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resolution: Mapped[str | None] = mapped_column(String(20))
    duration_sec: Mapped[float | None] = mapped_column(Numeric(8, 2))
    render_engine: Mapped[str | None] = mapped_column(String(100))

    project: Mapped["Project"] = relationship()
    asset: Mapped["Asset"] = relationship()


class Thumbnail(Base, UUIDPrimaryKeyMixin):
    """A candidate thumbnail produced by the Thumbnail Agent. Multiple rows
    per project support later A/B variant testing; exactly one is flagged
    `is_selected` for publishing.
    """

    __tablename__ = "thumbnails"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    variant_label: Mapped[str | None] = mapped_column(String(50))
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    project: Mapped["Project"] = relationship()
    asset: Mapped["Asset"] = relationship()
