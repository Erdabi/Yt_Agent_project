from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import ScriptStatus


class Script(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A version of a project's script. Scripts are versioned rows, never
    mutated in place — a QA-triggered regeneration produces a new version
    rather than overwriting the rejected one, so what changed stays visible.
    """

    __tablename__ = "scripts"

    project_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str | None] = mapped_column(String(100))
    target_duration_sec: Mapped[int | None] = mapped_column(Integer)
    word_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ScriptStatus] = mapped_column(
        Enum(ScriptStatus, name="script_status", native_enum=True),
        nullable=False,
        default=ScriptStatus.DRAFT,
    )

    project: Mapped["Project"] = relationship(back_populates="scripts")
    segments: Mapped[list["ScriptSegment"]] = relationship(
        back_populates="script", order_by="ScriptSegment.order_index"
    )


class ScriptSegment(Base, UUIDPrimaryKeyMixin):
    """A single timed section of a script (e.g. hook, a body beat, the
    CTA) — the unit the Storyboard and Voice-over agents each operate on.
    """

    __tablename__ = "script_segments"

    script_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_duration_sec: Mapped[int | None] = mapped_column(Integer)
    scene_notes: Mapped[str | None] = mapped_column(Text)

    script: Mapped["Script"] = relationship(back_populates="segments")
    storyboard_shots: Mapped[list["StoryboardShot"]] = relationship(
        back_populates="script_segment", order_by="StoryboardShot.order_index"
    )
    voiceovers: Mapped[list["Voiceover"]] = relationship(back_populates="script_segment")
