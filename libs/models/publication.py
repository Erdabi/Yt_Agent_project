from datetime import date, datetime

from sqlalchemy import ARRAY, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin
from libs.models.enums import PublishStatus


class Publication(Base, UUIDPrimaryKeyMixin):
    """The record of a project's upload to YouTube, produced by the
    Publisher Agent.
    """

    __tablename__ = "publications"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    youtube_video_id: Mapped[str | None] = mapped_column(String(32))
    #: Self-reported by the YouTube Data API response, never assumed from
    #: channel configuration — see libs/providers/youtube/base.py's
    #: `UploadResult.channel_id`.
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    #: The watch URL the Publisher Agent's provider returned alongside
    #: `youtube_video_id` — stored so nothing downstream has to
    #: reconstruct it from the video id.
    url: Mapped[str | None] = mapped_column(String(500))
    publish_status: Mapped[PublishStatus] = mapped_column(
        Enum(PublishStatus, name="publish_status", native_enum=True),
        nullable=False,
        default=PublishStatus.SCHEDULED,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the Publisher Agent's `upload_video` call completed — distinct
    #: from `published_at`, which is YouTube's own (possibly future,
    #: possibly still-absent) visibility-change time.
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    privacy_status: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(String)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    #: The playlist the video was actually added to, resolved by the
    #: Publisher Agent from channel configuration or a job override — the
    #: provider itself never invents this (see `VideoMetadata.playlist_id`).
    playlist_id: Mapped[str | None] = mapped_column(String(64))

    project: Mapped["Project"] = relationship(back_populates="publication")
    performance_metrics: Mapped[list["PerformanceMetric"]] = relationship(
        back_populates="publication"
    )


class PerformanceMetric(Base, UUIDPrimaryKeyMixin):
    """One day's performance snapshot for a publication, pulled by the
    Analytics Agent. One row per publication per day.
    """

    __tablename__ = "performance_metrics"
    __table_args__ = (
        # Time-series lookups and the eventual monthly range-partitioning
        # key (docs/architecture/04-database-design.md §4.3).
        Index("ix_performance_metrics_publication_date", "publication_id", "metric_date"),
    )

    publication_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("publications.id", ondelete="CASCADE"), nullable=False
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subscribers_gained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    watch_time_minutes: Mapped[float | None] = mapped_column(Numeric(12, 2))
    avg_view_duration_sec: Mapped[float | None] = mapped_column(Numeric(8, 2))
    impressions: Mapped[int | None] = mapped_column(Integer)
    ctr: Mapped[float | None] = mapped_column(Numeric(5, 4))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    publication: Mapped["Publication"] = relationship(back_populates="performance_metrics")
