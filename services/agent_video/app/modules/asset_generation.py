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
            )

        provider = self._get_cached_provider(planned.provider_capability)
        provider_name = type(provider).__name__
        physical_type, extension = _PHYSICAL_TYPE_AND_EXTENSION[planned.asset_type]

        cache_key = AssetCacheKey(
            capability=planned.provider_capability,
            provider_name=provider_name,
            asset_type=planned.asset_type.value,
            prompt=planned.description,
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
        )

    def _persist_storyboard_shot(self, planned: PlannedAsset, *, asset_id: str | None) -> None:
        with sync_session_scope() as session:
            session.add(
                StoryboardShot(
                    script_segment_id=UUID(planned.segment_id),
                    shot_type=planned.shot_type,
                    prompt_or_query=planned.description,
                    asset_id=UUID(asset_id) if asset_id else None,
                    order_index=planned.requirement_index,
                )
            )

    def _get_cached_provider(self, capability: str) -> Any:
        if capability not in self._provider_cache:
            self._provider_cache[capability] = get_provider(capability)
        return self._provider_cache[capability]

    @staticmethod
    def _call_provider(provider: Any, planned: PlannedAsset) -> bytes:
        if planned.provider_capability in ("image_gen", "video_gen"):
            return provider.generate(planned.description)
        # stock_media / audio_library: sourced from a library, not
        # generated from nothing.
        return provider.search(planned.description)
