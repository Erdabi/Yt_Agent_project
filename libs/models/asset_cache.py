from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from libs.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from libs.models.enums import AssetType


class AssetCacheEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Index of previously generated/sourced assets, keyed by a
    deterministic hash of the semantic request that produced them
    (see `services/agent_video/app/asset_cache.py`). Deliberately global
    rather than scoped to one project — two projects requesting the same
    prompt/provider/settings combination should reuse the same bytes, so
    this table lives independently of `Asset`/`projects` (no foreign key
    to either) and is never touched by a project's own cascade-delete.
    """

    __tablename__ = "asset_cache_entries"

    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    capability: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The requesting side's own asset-type vocabulary (e.g. a Script
    #: Agent `AssetType` value, or "narration" for TTS) — kept as a plain
    #: string since the cache itself doesn't care which typed vocabulary
    #: a caller uses, only `physical_asset_type` below does.
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    physical_asset_type: Mapped[AssetType] = mapped_column(
        Enum(AssetType, name="asset_type", native_enum=True), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    #: Capability-specific data a cache hit needs to fully reconstruct a
    #: typed result without recalling the provider — e.g. TTS's
    #: duration/word timings. Empty for capabilities where the stored
    #: bytes are themselves the whole answer.
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
