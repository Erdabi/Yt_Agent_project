"""Asset Generation module.

Second stage of the Video Agent's pipeline (see video_agent.py): executes
Asset Planning's plan by checking the Asset Cache (../asset_cache.py) for
each provider-generated requirement before calling the configured
provider, storing a fresh result on a cache miss, and persisting
`Asset`/`StoryboardShot` rows either way.

    Input:  list[PlannedAsset]
    Output: list[ResolvedAsset]

This is the *only* module that calls `libs.providers.get_provider` for
video/image/stock/audio capabilities — Asset Planning already decided
*which* capability each requirement needs, so swapping the concrete
provider behind `image_gen` (or any of the others) means editing
config/providers.yaml and touches nothing else in this pipeline: not
Asset Planning (which only knows capability names, not classes), not
Voice Generation (a different capability, `tts`, entirely), and not
Timeline Building/Rendering (which only ever see the `ResolvedAsset`
this module returns, never the provider that produced it).
"""

import hashlib
from typing import Any
from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset
from libs.models.enums import AssetType as PhysicalAssetType
from libs.models.storyboard import StoryboardShot
from libs.providers.registry import get_provider
from libs.schemas.script_production import AssetType as ScriptAssetType

from ..asset_cache import AssetCache, AssetCacheKey
from ..pipeline_schema import AssetResolutionKind, PlannedAsset, ResolvedAsset
from ..visual_prompt import DEFAULT_NEGATIVE_PROMPT

logger = get_logger(__name__)

#: Which physical media kind (libs.models.enums.AssetType — audio/image/
#: video/music/caption) a script-level requirement type produces, and
#: the file extension its bytes get stored under. Sound effects map to
#: AUDIO and background music cues to MUSIC — the one existing
#: distinction the physical AssetType enum already draws that matches a
#: script requirement type one-to-one.
_PHYSICAL_TYPE_AND_EXTENSION: dict[ScriptAssetType, tuple[PhysicalAssetType, str]] = {
    ScriptAssetType.AI_VIDEO: (PhysicalAssetType.VIDEO, "mp4"),
    ScriptAssetType.ANIMATION: (PhysicalAssetType.VIDEO, "mp4"),
    ScriptAssetType.STOCK_FOOTAGE: (PhysicalAssetType.VIDEO, "mp4"),
    ScriptAssetType.AI_IMAGE: (PhysicalAssetType.IMAGE, "png"),
    ScriptAssetType.DIAGRAM: (PhysicalAssetType.IMAGE, "png"),
    ScriptAssetType.MAP: (PhysicalAssetType.IMAGE, "png"),
    ScriptAssetType.PORTRAIT: (PhysicalAssetType.IMAGE, "png"),
    ScriptAssetType.SOUND_EFFECT: (PhysicalAssetType.AUDIO, "mp3"),
    ScriptAssetType.BACKGROUND_MUSIC_CUE: (PhysicalAssetType.MUSIC, "mp3"),
}


def _seed_for(variation_key: str) -> int:
    """A stable sampler seed derived from a beat's identity.

    Two beats that compose the same prompt would otherwise be generated
    with the same (provider-chosen random) seed only by chance — and a
    provider that defaults to a *fixed* seed would return visually
    identical frames for them. Deriving the seed from the beat's own
    `variation_key` makes distinct beats reliably distinct while keeping
    one beat reproducible across re-runs, which is the same property the
    cache key needs.
    """
    digest = hashlib.sha256(variation_key.encode("utf-8")).digest()
    # 32-bit: the range every sampler this codebase talks to accepts.
    return int.from_bytes(digest[:4], "big")


def _generation_kwargs(planned: PlannedAsset) -> dict[str, Any]:
    """Extra generation parameters passed to `image_gen`/`video_gen`.

    Both interfaces take `**kwargs` (libs/providers/{image_gen,video_gen}/base.py)
    and a provider is free to ignore anything it doesn't support, so this
    stays additive: a provider with no seed concept behaves exactly as
    before.
    """
    kwargs: dict[str, Any] = {"negative_prompt": DEFAULT_NEGATIVE_PROMPT}
    if planned.variation_key is not None:
        kwargs["seed"] = _seed_for(planned.variation_key)
    if planned.duration_sec is not None:
        # Only meaningful to video_gen; image providers ignore it.
        kwargs["duration_sec"] = planned.duration_sec
    return kwargs


class AssetGenerationModule:
    def __init__(self) -> None:
        self._cache = AssetCache()
        self._provider_cache: dict[str, Any] = {}

    def generate(self, project_id: str, planned_assets: list[PlannedAsset]) -> list[ResolvedAsset]:
        resolved = [self._resolve_one(project_id, planned) for planned in planned_assets]
        logger.info(
            "asset_generation_complete",
            project_id=project_id,
            planned_count=len(planned_assets),
            provider_generated_count=sum(
                1 for r in resolved if r.resolution_kind == AssetResolutionKind.PROVIDER_GENERATED
            ),
        )
        return resolved

    def _resolve_one(self, project_id: str, planned: PlannedAsset) -> ResolvedAsset:
        if planned.resolution_kind == AssetResolutionKind.RENDER_TIME_OVERLAY:
            # No provider call, no Asset row — Rendering composites
            # `description` directly. Still worth a StoryboardShot row
            # (it is a real shot in the visual sequence), just one whose
            # `asset_id` stays permanently null.
            self._persist_storyboard_shot(planned, asset_id=None)
            return ResolvedAsset(
                segment_id=planned.segment_id,
                order_index=planned.order_index,
                requirement_index=planned.requirement_index,
                asset_type=planned.asset_type,
                shot_type=planned.shot_type,
                is_audio=False,
                resolution_kind=planned.resolution_kind,
                asset_id=None,
                storage_path=None,
                provider_name=None,
                description=planned.description,
                beat_index=planned.beat_index,
                start_sec=planned.start_sec,
                duration_sec=planned.duration_sec,
            )

        provider = self._get_cached_provider(planned.provider_capability)
        provider_name = type(provider).__name__
        physical_type, extension = _PHYSICAL_TYPE_AND_EXTENSION[planned.asset_type]

        cache_key = AssetCacheKey(
            capability=planned.provider_capability,
            provider_name=provider_name,
            asset_type=planned.asset_type.value,
            prompt=planned.description,
            # Set for per-beat visuals, `None` for segment-wide assets —
            # see `AssetCacheKey.variation_key`. This is what stops two
            # beats that happen to compose the same prompt from sharing
            # one image file and making the video visibly repeat.
            variation_key=planned.variation_key,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            storage_path = cached.storage_path
            # Attribute to whichever provider actually produced these
            # bytes, which may not be today's configured provider for
            # this capability.
            provider_name = cached.provider_name
        else:
            data = self._call_provider(provider, planned)
            storage_path = self._cache.put(
                cache_key,
                data=data,
                extension=extension,
                physical_asset_type=physical_type,
                provider_name=provider_name,
            )

        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=physical_type,
                provider=provider_name,
                storage_path=storage_path,
                metadata_={
                    "segment_id": planned.segment_id,
                    "requirement_index": planned.requirement_index,
                    "script_asset_type": planned.asset_type.value,
                    "description": planned.description,
                },
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

        if not planned.is_audio:
            self._persist_storyboard_shot(planned, asset_id=asset_id)

        return ResolvedAsset(
            segment_id=planned.segment_id,
            order_index=planned.order_index,
            requirement_index=planned.requirement_index,
            asset_type=planned.asset_type,
            shot_type=planned.shot_type,
            is_audio=planned.is_audio,
            resolution_kind=planned.resolution_kind,
            asset_id=asset_id,
            storage_path=storage_path,
            provider_name=provider_name,
            description=planned.description,
            beat_index=planned.beat_index,
            start_sec=planned.start_sec,
            duration_sec=planned.duration_sec,
        )

    def _persist_storyboard_shot(self, planned: PlannedAsset, *, asset_id: str | None) -> None:
        with sync_session_scope() as session:
            session.add(
                StoryboardShot(
                    script_segment_id=UUID(planned.segment_id),
                    shot_type=planned.shot_type,
                    prompt_or_query=planned.description,
                    asset_id=UUID(asset_id) if asset_id else None,
                    # A segment's visuals are now ordered by beat, not by
                    # which requirement they came from — several beats
                    # routinely share one requirement, so
                    # `requirement_index` is no longer unique within a
                    # segment and would collide here.
                    order_index=(
                        planned.beat_index
                        if planned.beat_index is not None
                        else planned.requirement_index
                    ),
                )
            )

    def _get_cached_provider(self, capability: str) -> Any:
        if capability not in self._provider_cache:
            self._provider_cache[capability] = get_provider(capability)
        return self._provider_cache[capability]

    @staticmethod
    def _call_provider(provider: Any, planned: PlannedAsset) -> bytes:
        if planned.provider_capability in ("image_gen", "video_gen"):
            return provider.generate(
                planned.description, **_generation_kwargs(planned)
            )
        # stock_media / audio_library: sourced from a library, not
        # generated from nothing.
        return provider.search(planned.description)
