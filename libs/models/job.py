from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import JobStatus


class Job(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The execution/audit log: every agent invocation, without exception.

    This is what the agent framework (libs/agents/base.py) reads and
    writes on every run — it is simultaneously the audit trail, the retry
    mechanism, and the primary debugging tool. `project_id` is nullable
    because channel-level jobs (e.g. an ideation run) aren't tied to a
    single project.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # The stuck-job sweep's primary query
        # (docs/architecture/04-database-design.md §4.3).
        Index("ix_jobs_status_queue", "status", "queue_name"),
    )

    project_id: Mapped["UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", native_enum=True),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project | None"] = relationship(back_populates="jobs")
