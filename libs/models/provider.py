from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from libs.models.base import Base, UUIDPrimaryKeyMixin
from libs.models.enums import ProviderCapability


class ProviderConfig(Base, UUIDPrimaryKeyMixin):
    """Which concrete AI provider backs each capability, in priority order.

    This table is what makes providers swappable at runtime: an agent asks
    `libs.providers.<capability>.registry` for "the active provider for
    channel X", never for a specific vendor by name. `secret_ref` holds the
    *name* of the environment variable containing the API key — never the
    key itself; see docs/architecture/04-database-design.md §4.4.
    """

    __tablename__ = "provider_configs"

    channel_id: Mapped["UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    capability: Mapped[ProviderCapability] = mapped_column(
        Enum(ProviderCapability, name="provider_capability", native_enum=True),
        nullable=False,
    )
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(255))

    channel: Mapped["Channel | None"] = relationship(back_populates="provider_configs")
    usage_logs: Mapped[list["ProviderUsageLog"]] = relationship(
        back_populates="provider_config"
    )


class ProviderUsageLog(Base, UUIDPrimaryKeyMixin):
    """Per-call usage/cost record, for spend attribution by provider and by
    project.
    """

    __tablename__ = "provider_usage_log"

    provider_config_id: Mapped["UUID"] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped["UUID | None"] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    cost_estimate_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    provider_config: Mapped["ProviderConfig"] = relationship(back_populates="usage_logs")
