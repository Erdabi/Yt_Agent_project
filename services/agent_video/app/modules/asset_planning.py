"""Asset Planning module.

Runs after Visual Beat Planning inside the Video Agent's pipeline (see
video_agent.py): decides which concrete production assets are needed and
how each should be satisfied — turning the Script Agent's
provider-independent `asset_requirements`
(libs/schemas/script_production.py) into a plan Asset Generation can
execute.

    Input:  list[SegmentInput], list[VisualBeat]
    Output: list[PlannedAsset]   (one per resolvable asset)

Two kinds of asset are planned here, from two different sources:

- **Per-beat visuals.** Visual Beat Planning has already subdivided each
  segment's narration into the visual moments it needs, sized from the
  real narration duration; this module turns each of those into one
  planned asset with its own composed prompt (../visual_prompt.py) and
  its own on-screen duration.
- **Segment-wide assets.** Sound effects, background-music cues, and
  render-time text overlays aren't per-beat — they belong to the segment
  as a whole — so they still get exactly one planned asset per declared
  requirement, as before.

Makes no provider calls and writes nothing to the database — deciding
*what* is needed and *how* it maps to a `libs.providers` capability is
pure computation over data already in memory (`ASSET_TYPE_ROUTING`, see
pipeline_schema.py), so this module is fully real and testable without
any external dependency. Swapping which concrete provider satisfies
"image_gen" never touches this module at all — it only ever decides
*that* a requirement needs `image_gen`, never which class answers that
call.
"""

from libs.core.logging import get_logger
from libs.schemas.script_production import VISUAL_ASSET_TYPES

from ..pipeline_schema import (
    ASSET_TYPE_ROUTING,
    AUDIO_ASSET_TYPES,
    AssetResolutionKind,
    PlannedAsset,
    SegmentInput,
    VisualBeat,
)
from ..visual_prompt import compose_visual_prompt

logger = get_logger(__name__)


class AssetPlanningModule:
    def plan(
        self, segments: list[SegmentInput], visual_beats: list[VisualBeat]
    ) -> list[PlannedAsset]:
        segments_by_id = {segment.segment_id: segment for segment in segments}
        planned: list[PlannedAsset] = []

        for beat in visual_beats:
            segment = segments_by_id.get(beat.segment_id)
            if segment is None:
                raise ValueError(
                    f"visual beat references unknown segment {beat.segment_id!r} — Asset "
                    "Planning and Visual Beat Planning must be given the same segment list"
                )
            planned.append(self._planned_visual(beat, segment))

        for segment in segments:
            planned.extend(self._planned_segment_wide(segment))

        logger.info(
            "asset_planning_complete",
            segment_count=len(segments),
            planned_count=len(planned),
            per_beat_visual_count=len(visual_beats),
        )
        return planned

    @staticmethod
    def _planned_visual(beat: VisualBeat, segment: SegmentInput) -> PlannedAsset:
        provider_capability, shot_type, resolution_kind = ASSET_TYPE_ROUTING[beat.asset_type]
        return PlannedAsset(
            segment_id=beat.segment_id,
            order_index=beat.order_index,
            requirement_index=beat.requirement_index,
            asset_type=beat.asset_type,
            description=compose_visual_prompt(beat, segment),
            resolution_kind=resolution_kind,
            provider_capability=provider_capability,
            shot_type=shot_type,
            is_audio=False,
            beat_index=beat.beat_index,
            start_sec=beat.start_sec,
            duration_sec=beat.duration_sec,
            # Deterministic and unique per beat — see
            # `PlannedAsset.variation_key` for why a per-beat identity is
            # what keeps two beats from collapsing onto the same cached
            # image and making the finished video visibly repeat.
            variation_key=f"{beat.segment_id}:{beat.beat_index}",
        )

    @staticmethod
    def _planned_segment_wide(segment: SegmentInput) -> list[PlannedAsset]:
        """Requirements that belong to the segment as a whole rather than
        to any one visual beat: audio (sound effects, music cues) and
        render-time text overlays. Provider-generated *visual*
        requirements are deliberately skipped here — Visual Beat Planning
        already expanded those into per-beat assets, so planning them
        again would double-generate every visual.
        """
        planned: list[PlannedAsset] = []
        for requirement_index, requirement in enumerate(segment.production.asset_requirements):
            routing = ASSET_TYPE_ROUTING.get(requirement.asset_type)
            if routing is None:
                # subtitle_emphasis: not a planned asset at all —
                # Subtitle Generation reads it directly off the
                # segment's own asset_requirements instead.
                continue
            provider_capability, shot_type, resolution_kind = routing
            is_per_beat_visual = (
                requirement.asset_type in VISUAL_ASSET_TYPES
                and resolution_kind is AssetResolutionKind.PROVIDER_GENERATED
            )
            if is_per_beat_visual:
                continue
            planned.append(
                PlannedAsset(
                    segment_id=segment.segment_id,
                    order_index=segment.order_index,
                    requirement_index=requirement_index,
                    asset_type=requirement.asset_type,
                    description=requirement.description,
                    resolution_kind=resolution_kind,
                    provider_capability=provider_capability,
                    shot_type=shot_type,
                    is_audio=requirement.asset_type in AUDIO_ASSET_TYPES,
                )
            )
        return planned
