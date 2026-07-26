from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import ScriptSegmentType, ScriptStatus


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
    # The narrative structure/story arc the Script Agent deliberately
    # chose (e.g. "problem -> agitation -> solution") and the concrete
    # retention techniques it wove through the script (open loops,
    # pattern interrupts, callbacks) and where — both cross-cutting, so
    # they live on the script as a whole rather than on any one segment.
    structure_notes: Mapped[str | None] = mapped_column(Text)
    retention_notes: Mapped[str | None] = mapped_column(Text)
    # What the Script Agent's self-review pass checked and changed (or
    # why review was skipped) — see
    # services/agent_scriptwriter/app/script_reviewer.py. Nullable only
    # for rows written before this column existed; every script produced
    # by that module always sets it, even to a "skipped: <reason>" note.
    review_notes: Mapped[str | None] = mapped_column(Text)
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
    # Which structural beat this is (hook, introduction, a main body
    # section, ending, call to action) — see libs/models/enums.py.
    segment_type: Mapped[ScriptSegmentType] = mapped_column(
        Enum(ScriptSegmentType, name="script_segment_type", native_enum=True),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_duration_sec: Mapped[int | None] = mapped_column(Integer)
    scene_notes: Mapped[str | None] = mapped_column(Text)
    # Concrete visual/b-roll/on-screen-graphic ideas for this beat —
    # distinct from `scene_notes` (what's happening on screen); this is
    # what it should look like. The Storyboard module
    # (services/agent_video/app/modules/storyboard.py) resolves these
    # into concrete assets once implemented.
    visual_notes: Mapped[str | None] = mapped_column(Text)
    # Structured production metadata for this beat — camera framing,
    # visual asset type, transition, pacing, narration emotion, emphasis
    # words, estimated speech speed, on-screen text — validated against
    # services/agent_scriptwriter/app/script_schema.py's
    # `SegmentProductionMetadata` on the way in. JSONB rather than one
    # column per field: the Video Agent always reads this whole bundle
    # together for one beat, it never filters segments by an individual
    # field via SQL, so there's nothing a relational column would buy
    # over a single validated blob (same reasoning as
    # `Channel.persona_config`).
    production_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    script: Mapped["Script"] = relationship(back_populates="segments")
    storyboard_shots: Mapped[list["StoryboardShot"]] = relationship(
        back_populates="script_segment", order_by="StoryboardShot.order_index"
    )
    voiceovers: Mapped[list["Voiceover"]] = relationship(back_populates="script_segment")
