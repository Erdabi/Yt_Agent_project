from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from libs.models.base import Base, UUIDPrimaryKeyMixin


class SystemEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only audit log for anything not already covered by `jobs` —
    approval decisions, config changes, manual overrides, provider
    fallback/circuit-breaker trips.
    """

    __tablename__ = "system_events"

    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped["UUID | None"] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
