from sqlalchemy import ARRAY, Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import CompetitionLevel, IdeaStatus

# Dimension for the embedding model used to score/dedup ideas
# (text-embedding-3-small and comparable models are 1536-dim). Revisit
# via migration if the embedding provider changes.
EMBEDDING_DIM = 1536


class VideoIdea(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A candidate topic produced by the Research Agent, before it becomes
    a `Project`.
    """

    __tablename__ = "video_ideas"
    __table_args__ = (
        # Approximate nearest-neighbor index backing the Research Agent's
        # semantic dedup check (docs/architecture/04-database-design.md §4.3).
        Index(
            "ix_video_ideas_embedding_ivfflat",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"lists": "100"},
        ),
    )

    channel_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(6, 3))
    # The Research Agent's answer to "why would people watch this" — set
    # alongside the other research fields below (services/agent_research).
    rationale: Mapped[str | None] = mapped_column(Text)
    target_audience: Mapped[str | None] = mapped_column(Text)
    suggested_angle: Mapped[str | None] = mapped_column(Text)
    competition_level: Mapped[CompetitionLevel | None] = mapped_column(
        Enum(CompetitionLevel, name="competition_level", native_enum=True)
    )
    suggested_length_sec: Mapped[int | None] = mapped_column(Integer)
    # Free-text research context: trend signals considered, tradeoffs,
    # duplicate-content warnings — everything that informed the score but
    # doesn't fit a single structured column.
    research_notes: Mapped[str | None] = mapped_column(Text)
    # pgvector column for semantic-similarity dedup against past ideas/videos.
    # Stays null until a real embedding provider is wired in — the Research
    # Agent's current duplicate check is a lexical heuristic instead (see
    # services/agent_research/app/dedup.py).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    status: Mapped[IdeaStatus] = mapped_column(
        Enum(IdeaStatus, name="idea_status", native_enum=True),
        nullable=False,
        default=IdeaStatus.PROPOSED,
    )

    channel: Mapped["Channel"] = relationship(back_populates="video_ideas")
    project: Mapped["Project | None"] = relationship(back_populates="idea")
