"""Asset Cache: a provider-aware, provider-implementation-independent
cache sitting in front of every `libs.providers` call the Video Agent's
pipeline makes (services/agent_video/app/modules/asset_generation.py and
voice_generation.py — the only two modules that ever call a provider; see
pipeline_schema.py).

Every generated/sourced asset is keyed by a deterministic hash of its
*semantic request* — capability, provider, prompt, and whatever settings
(resolution, duration, style, language) shaped it — computed identically
regardless of which project asked or which provider is active today.
Before a module calls out to a provider, it checks this cache; a hit
means the provider is never called at all.

Deliberately global, not scoped to one project: the same prompt/provider
combination means the same bytes no matter which project requested them,
so caching per-project would throw away most of the dedup value. Cached
bytes live under their own storage namespace (`_CACHE_STORAGE_NAMESPACE`,
never a real project id), and every caller — hit or miss — points its own
`Asset.storage_path` directly at that namespace rather than making a
project-local copy. `Asset` rows themselves are still created per project
on every request; only the underlying bytes are shared.

The cache mechanism itself stays fully generic — `capability` and
`provider_name` are plain data fields on the key/entry, not branches in
this file, so nothing here needs to know about any concrete provider.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset_cache import AssetCacheEntry
from libs.models.enums import AssetType as PhysicalAssetType
from libs.storage import get_storage_backend

logger = get_logger(__name__)

#: Storage namespace for cached bytes — used as the `project_id` argument
#: to `StorageBackend.save_bytes()`, but deliberately not a real project:
#: cached assets outlive and are shared across whichever projects request
#: them, so they must never live under one project's own directory.
_CACHE_STORAGE_NAMESPACE = "_asset_cache"
_CACHE_STORAGE_CATEGORY = "assets"


@dataclass(frozen=True)
class AssetCacheKey:
    """The semantic identity of a provider request. Two requests that
    produce equal `compute_hash()` values are considered interchangeable
    — the second one should reuse the first's stored bytes instead of
    calling the provider again.
    """

    capability: str
    provider_name: str
    asset_type: str
    prompt: str
    resolution: str | None = None
    duration_sec: float | None = None
    style: str | None = None
    #: No per-channel/per-project language configuration exists yet
    #: (docs/architecture) — defaults to a constant rather than `None` so
    #: today's single implicit language is honestly hashed as a baseline,
    #: ready to matter once real language settings exist.
    language: str = "en"
    #: An explicit "these must not share bytes" discriminator, for
    #: requests that are semantically identical but whose *outputs* must
    #: still differ. Content-addressing is the right default — the same
    #: request really should reuse the same bytes — but it is wrong for
    #: two visuals shown at different moments of the same video: they
    #: would collapse onto one image file and the video would visibly
    #: repeat. A caller that needs distinct output passes a stable,
    #: deterministic discriminator here (the Video Agent uses the beat's
    #: `segment_id:beat_index`), so re-running the same job still hits
    #: the cache while genuinely different beats never do. Left `None`
    #: wherever sharing is correct, which keeps every existing key's hash
    #: unchanged.
    variation_key: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    def compute_hash(self) -> str:
        payload = {
            "capability": self.capability,
            "provider_name": self.provider_name,
            "asset_type": self.asset_type,
            "prompt": self.prompt,
            "resolution": self.resolution,
            "duration_sec": self.duration_sec,
            "style": self.style,
            "language": self.language,
            "settings": self.settings,
        }
        # Only added to the hashed payload when actually set, so keys
        # that don't need output separation hash exactly as they did
        # before this field existed — existing cache entries stay valid.
        if self.variation_key is not None:
            payload["variation_key"] = self.variation_key
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedAsset:
    """What a cache hit returns — everything a module needs to build its
    own `Asset` row (and, for TTS, its own typed result) without calling
    the provider.
    """

    storage_path: str
    physical_asset_type: PhysicalAssetType
    #: Which provider originally produced these bytes — may not be
    #: today's configured provider for this capability, and that's fine:
    #: it's recorded for accurate `Asset.provider` attribution, not used
    #: to decide anything.
    provider_name: str
    metadata: dict[str, Any]


class AssetCache:
    def __init__(self) -> None:
        self._storage = get_storage_backend()

    def get(self, key: AssetCacheKey) -> CachedAsset | None:
        cache_key_hash = key.compute_hash()
        with sync_session_scope() as session:
            entry = session.scalar(
                select(AssetCacheEntry).where(AssetCacheEntry.cache_key == cache_key_hash)
            )
            if entry is None:
                return None
            entry.hit_count += 1
            entry.last_used_at = datetime.now(timezone.utc)
            cached = CachedAsset(
                storage_path=entry.storage_path,
                physical_asset_type=entry.physical_asset_type,
                provider_name=entry.provider_name,
                metadata=dict(entry.metadata_),
            )
        logger.info(
            "asset_cache_hit",
            capability=key.capability,
            cache_key=cache_key_hash,
        )
        return cached

    def put(
        self,
        key: AssetCacheKey,
        *,
        data: bytes,
        extension: str,
        physical_asset_type: PhysicalAssetType,
        provider_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store newly generated bytes for `key` and return the storage
        path callers should use for their own `Asset.storage_path` — both
        now and for every future cache hit on this same key.
        """
        cache_key_hash = key.compute_hash()
        storage_path = self._storage.save_bytes(
            _CACHE_STORAGE_NAMESPACE,
            _CACHE_STORAGE_CATEGORY,
            f"{cache_key_hash}.{extension}",
            data,
        )
        try:
            with sync_session_scope() as session:
                session.add(
                    AssetCacheEntry(
                        cache_key=cache_key_hash,
                        capability=key.capability,
                        provider_name=provider_name,
                        asset_type=key.asset_type,
                        physical_asset_type=physical_asset_type,
                        storage_path=storage_path,
                        metadata_=metadata or {},
                    )
                )
        except IntegrityError:
            # Lost a race with another worker caching the same key
            # concurrently — our own bytes are already written and valid
            # for the caller to use regardless of whether our index row
            # made it in; the same "observability must never break real
            # work" tolerance as libs/llm_usage/tracker.py.
            logger.debug("asset_cache_put_race_lost", cache_key=cache_key_hash)
        return storage_path
