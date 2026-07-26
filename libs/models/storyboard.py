from sqlalchemy import Enum, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin
from libs.models.enums import ShotType


class StoryboardShot(Base, UUIDPrimaryKeyMixin):
    """One planned visual for a script segment — either sourced from stock
    media or generated — produced by the Video Agent's Asset Generation
    module (services/agent_video/app/modules/asset_generation.py).
    """

    __tablename__ = "storyboard_shots"

    script_segment_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("script_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    shot_type: Mapped[ShotType] = mapped_column(
        Enum(ShotType, name="shot_type", native_enum=True), nullable=False
    )
    prompt_or_query: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable: a shot starts as a plan and is only resolved to a concrete
    # asset once stock media is found or a generation call completes.
    asset_id: Mapped["UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)

    script_segment: Mapped["ScriptSegment"] = relationship(
        back_populates="storyboard_shots"
    )
    asset: Mapped["Asset | None"] = relationship()
