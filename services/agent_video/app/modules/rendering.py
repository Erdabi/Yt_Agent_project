"""Rendering module.

Final stage of the Video Agent's pipeline (see video_agent.py): turns a
`Timeline` into the actual rendered video.

    Input:  Timeline, ChannelBranding
    Output: RenderResult

Building the render plan from the Timeline — resolving each entry's
visual (looped/trimmed to its narration-driven duration), layering the
voice-over, supplementary audio, and captions, applying the segment's own
transition/pacing — is real, testable logic with no external dependency.
Actually invoking a compositor is not: ffmpeg is deliberately not
installed in this image yet (see services/agent_video/requirements.txt),
so that one leaf call stays an honest `NotImplementedError` until it is —
matching every other Video Agent module's stance on real vendor
integrations it doesn't have yet. Rendering never imports
`libs.providers` at all: every asset it needs was already resolved by
Asset Generation/Voice Generation, so it has nothing to swap and nothing
to know about which vendor produced any of it.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Render
from libs.models.enums import AssetType as PhysicalAssetType

from ..pipeline_schema import ChannelBranding, RenderResult, Timeline, TimelineEntry

logger = get_logger(__name__)


@dataclass(frozen=True)
class RenderOperation:
    """One instruction in the render plan for a single timeline entry —
    kept generic rather than tied to any one compositor's own API (an
    ffmpeg filtergraph, a moviepy call, ...), the same reason this
    pipeline's other contracts (pipeline_schema.py) never mention a
    vendor by name.
    """

    segment_id: str
    start_sec: float
    end_sec: float
    visual_storage_paths: list[str]
    overlay_texts: list[str]
    voice_storage_path: str
    supplementary_audio_storage_paths: list[str]
    subtitle_lines: list[dict[str, Any]]
    transition_type: str
    pacing: str


@dataclass(frozen=True)
class RenderPlan:
    project_id: str
    operations: list[RenderOperation]
    total_duration_sec: float
    intro_asset_path: str | None
    outro_asset_path: str | None
    caption_style: str | None


class RenderingModule:
    def render(self, project_id: str, timeline: Timeline, branding: ChannelBranding) -> RenderResult:
        plan = self._build_plan(project_id, timeline, branding)
        result = self._execute(plan)
        self._persist(project_id, result)
        return result

    def _build_plan(
        self, project_id: str, timeline: Timeline, branding: ChannelBranding
    ) -> RenderPlan:
        operations = [self._operation_for_entry(entry) for entry in timeline.entries]
        logger.info(
            "render_plan_built",
            project_id=project_id,
            operation_count=len(operations),
            total_duration_sec=timeline.total_duration_sec,
        )
        return RenderPlan(
            project_id=project_id,
            operations=operations,
            total_duration_sec=timeline.total_duration_sec,
            intro_asset_path=branding.intro_asset_path,
            outro_asset_path=branding.outro_asset_path,
            caption_style=branding.caption_style,
        )

    @staticmethod
    def _operation_for_entry(entry: TimelineEntry) -> RenderOperation:
        visual_storage_paths = [
            asset.storage_path for asset in entry.visual_assets if asset.storage_path
        ]
        # A RENDER_TIME_OVERLAY asset (text_overlay) has no storage_path
        # — its description is composited directly as on-screen text.
        overlay_texts = [
            asset.description for asset in entry.visual_assets if asset.storage_path is None
        ]
        return RenderOperation(
            segment_id=entry.segment_id,
            start_sec=entry.start_sec,
            end_sec=entry.end_sec,
            visual_storage_paths=visual_storage_paths,
            overlay_texts=overlay_texts,
            voice_storage_path=entry.voice_asset.storage_path,
            supplementary_audio_storage_paths=[
                asset.storage_path for asset in entry.supplementary_audio if asset.storage_path
            ],
            subtitle_lines=[
                {
                    "start_sec": cue.start_sec,
                    "end_sec": cue.end_sec,
                    "text": cue.text,
                    "emphasized": cue.emphasized,
                }
                for cue in entry.subtitle_cues
            ],
            transition_type=entry.transition_type.value,
            pacing=entry.pacing.value,
        )

    def _execute(self, plan: RenderPlan) -> RenderResult:
        raise NotImplementedError(
            "Rendering module's compositor invocation lands in Phase 1 of the "
            "roadmap (docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4. The render "
            f"plan itself built successfully ({len(plan.operations)} operations, "
            f"{plan.total_duration_sec:.1f}s total) — only invoking ffmpeg/a "
            "compositor on it is not implemented yet."
        )

    @staticmethod
    def _persist(project_id: str, result: RenderResult) -> None:
        """Not reachable until `_execute` above produces a real
        `RenderResult` instead of raising — written now, alongside every
        other module's own persistence, rather than left for later, so
        Rendering follows the same "the module that produces an asset
        persists it" convention Asset Generation/Voice Generation already
        do.
        """
        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.VIDEO,
                provider=result.render_engine,
                storage_path=result.storage_path,
                duration_sec=result.duration_sec,
                metadata_={"resolution": result.resolution},
            )
            session.add(asset)
            session.flush()

            session.add(
                Render(
                    project_id=UUID(project_id),
                    asset_id=asset.id,
                    resolution=result.resolution,
                    duration_sec=result.duration_sec,
                    render_engine=result.render_engine,
                )
            )
