from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin


class QAReport(Base, UUIDPrimaryKeyMixin):
    """The QA Agent's verdict on one stage's output. `issues` is a JSONB
    list of `{category, severity, detail}` objects — the Orchestrator reads
    `category` to decide which upstream stage a failure routes back to.
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
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project"] = relationship(back_populates="qa_reports")
