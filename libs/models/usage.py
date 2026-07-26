from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin


class LLMUsageLog(Base, UUIDPrimaryKeyMixin):
    """One row per LLM API call — provider, model, prompt version, token
    counts, elapsed time, estimated cost — recorded by
    `libs.llm_usage.track_llm_call` for every Claude call this codebase
    makes (reasoning.py, idea_generator.py, knowledge_builder.py), so
    there is an actual history to analyze/optimize spend against instead
    of no record at all.

    Deliberately a separate table from `ProviderUsageLog`
    (libs/models/provider.py): that table is scoped to the DB-driven,
    swappable-vendor `libs.providers` system (video/TTS/image/YouTube),
    keyed by a `provider_configs` row — none of this table's call sites
    have one, since they all call the Anthropic SDK directly. `project_id`
    is nullable with `ondelete="SET NULL"`, matching
    `ProviderUsageLog.project_id`'s precedent: usage/cost history is meant
    to outlive project deletion for aggregate analytics.
    """

    __tablename__ = "llm_usage_log"

    project_id: Mapped["UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    call_site: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="anthropic")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_estimate_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project | None"] = relationship()
