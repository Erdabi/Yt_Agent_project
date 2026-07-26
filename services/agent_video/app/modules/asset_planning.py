"""Asset Planning module.

First stage of the Video Agent's pipeline (see video_agent.py): decides,
per script segment, which concrete production assets are needed and how
each should be satisfied — turning the Script Agent's provider-independent
`asset_requirements` (libs/schemas/script_production.py) into a plan
Asset Generation can execute.

    Input:  list[SegmentInput]   (one per script segment, in order)
    Output: list[PlannedAsset]   (one per resolvable asset requirement)

Makes no provider calls and writes nothing to the database — deciding
*what* is needed and *how* it maps to a `libs.providers` capability is
pure computation over data already in memory (`ASSET_TYPE_ROUTING`, see
pipeline_schema.py), so this module is fully real and testable without
any external dependency, unlike every other module in this pipeline.
Swapping which concrete provider satisfies "image_gen" never touches
this module at all — it only ever decides *that* a requirement needs
`image_gen`, never which class answers that call.
"""

from libs.core.logging import get_logger

from ..pipeline_schema import (
    ASSET_TYPE_ROUTING,
    AUDIO_ASSET_TYPES,
    PlannedAsset,
    SegmentInput,
)

logger = get_logger(__name__)


class AssetPlanningModule:
    def plan(self, segments: list[SegmentInput]) -> list[PlannedAsset]:
        planned: list[PlannedAsset] = []
        for segment in segments:
            for requirement_index, requirement in enumerate(segment.production.asset_requirements):
                routing = ASSET_TYPE_ROUTING.get(requirement.asset_type)
                if routing is None:
                    # subtitle_emphasis: not a planned asset at all —
                    # Subtitle Generation reads it directly off the
                    # segment's own asset_requirements instead.
                    continue
                provider_capability, shot_type, resolution_kind = routing
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
        logger.info(
            "asset_planning_complete",
            segment_count=len(segments),
            planned_count=len(planned),
        )
        return planned
