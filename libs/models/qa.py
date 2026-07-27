from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin


class QAReport(Base, UUIDPrimaryKeyMixin):
    """The QA Agent's verdict on one stage's output. `issues` is a JSONB
    list of `{category, severity, detail, suggested_fix}` objects — the
    Orchestrator reads `category` to decide which upstream stage a
    failure routes back to.
    """

    __tablename__ = "qa_reports"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issues: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Per-reviewer review metadata — which reviewers ran, each one's own
    # passed/summary, and which llm provider/model produced any LLM-based
    # finding (services/agent_qa/app/quality_control_agent.py) — kept
    # separate from `issues` (the flat, category-tagged findings list
    # `passed` is derived from) so a consumer can render one without
    # parsing the other.
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="qa_reports")
