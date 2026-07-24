from sqlalchemy import Enum, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from libs.models.enums import ProjectStage, ProjectStatus


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The central pipeline-instance row. One `Project` exists per video,
    from an approved idea through to a published upload. `current_stage`
    and `status` are owned exclusively by the Orchestrator (see
    docs/architecture/01-system-architecture.md §1.4) — agents never write
    these columns themselves.
    """

    __tablename__ = "projects"
    __table_args__ = (
        # The Orchestrator's primary polling query
        # (docs/architecture/04-database-design.md §4.3).
        Index("ix_projects_status_stage", "status", "current_stage"),
    )

    channel_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idea_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("video_ideas.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    current_stage: Mapped[ProjectStage] = mapped_column(
        Enum(ProjectStage, name="project_stage", native_enum=True),
        nullable=False,
        default=ProjectStage.IDEATION,
    )
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status", native_enum=True),
        nullable=False,
        default=ProjectStatus.PENDING,
    )
    # Bounds the FAILED_QA -> upstream-stage loop-back described in the
    # pipeline state machine, so a persistently broken generation can't
    # retry forever.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    channel: Mapped["Channel"] = relationship(back_populates="projects")
    idea: Mapped["VideoIdea"] = relationship(back_populates="project")
    scripts: Mapped[list["Script"]] = relationship(back_populates="project")
    assets: Mapped[list["Asset"]] = relationship(back_populates="project")
    qa_reports: Mapped[list["QAReport"]] = relationship(back_populates="project")
    jobs: Mapped[list["Job"]] = relationship(back_populates="project")
    publication: Mapped["Publication | None"] = relationship(back_populates="project")
