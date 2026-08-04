"""Rendering module.

Final stage of the Video Agent's pipeline (see video_agent.py): turns a
`Timeline` into the actual rendered video.

    Input:  Timeline, ChannelBranding
    Output: RenderResult

Building the render plan from the Timeline — resolving each entry's
visual (looped/trimmed to its narration-driven duration), layering the
voice-over, supplementary audio, and captions, applying the segment's own
transition/pacing — is real, testable logic with no external dependency.
Actually compositing that plan into a video is now real too: Rendering
calls `libs.providers.get_provider("editor")` — the *only* capability it
needs — exactly the way Asset Generation/Voice Generation call their own
capabilities, so swapping the compositor (e.g. a future Remotion-based
provider) means editing `config/providers.yaml`, not this module. Every
other asset this module needs was already resolved by Asset Generation/
Voice Generation; Rendering reads their bytes back via `libs.storage` to
hand to the provider, and writes the provider's output back through the
same abstraction. No FFmpeg (or any compositor) logic lives here — that
stays entirely inside `libs/providers/editor/`; this module only builds
the generic `EditSpec`/`RenderPlan` and resolves storage paths to bytes.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Render
from libs.models.enums import AssetType as PhysicalAssetType
from libs.models.enums import ShotType
from libs.providers.editor.base import (
    EditorInputError,
    EditResult,
    EditSegment,
    EditSpec,
    ProgressCallback,
    RenderProgress,
    SubtitleLine,
    VisualClip,
)
from libs.providers.registry import get_provider
from libs.storage import get_storage_backend

from ..pipeline_schema import ChannelBranding, RenderResult, Timeline, TimelineEntry

logger = get_logger(__name__)

#: Which `ShotType` values are a moving video file vs. a static image —
#: used to tell the editor provider how to treat each visual clip's
#: bytes (loop-to-duration for either, but a video source shouldn't be
#: frozen the way an image is). `TEXT_OVERLAY` never reaches here: a
#: `RENDER_TIME_OVERLAY` asset has no `storage_path` (see
#: AssetGenerationModule), so it never becomes a `VisualClipRef`.
_VIDEO_SHOT_TYPES = frozenset({ShotType.STOCK, ShotType.AI_VIDEO})

#: Default output-format profile (libs/providers/editor/profiles.py) —
#: today's only real caller (video_agent.py) never varies this, but
#: `render()` accepts a different one so a future Shorts/aspect-ratio
#: job is a different argument, not a rewrite of this module or the
#: compositor.
_DEFAULT_PROFILE_NAME = "long_form_1080p"


@dataclass(frozen=True)
class VisualClipRef:
    """One visual asset still identified by its storage path — Rendering
    resolves these to actual bytes right before handing them to the
    editor provider, not while building the plan, so plan-building itself
    stays a pure, storage-free operation like every other module's.
    """

    storage_path: str
    kind: str  # "image" | "video"
    #: How long this specific visual holds the screen, from the visual
    #: beat it realizes. `None` for a visual with no beat of its own, in
    #: which case the compositor falls back to dividing the segment
    #: evenly across however many visuals it has.
    duration_sec: float | None = None


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
    visual_clips: list[VisualClipRef]
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
    music_bed_path: str | None
    caption_style: str | None


class RenderingModule:
    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._provider = get_provider("editor")

    def render(
        self,
        project_id: str,
        timeline: Timeline,
        branding: ChannelBranding,
        *,
        profile_name: str = _DEFAULT_PROFILE_NAME,
    ) -> RenderResult:
        plan = self._build_plan(project_id, timeline, branding)
        edit_result = self._execute(plan, profile_name)
        render_engine = type(self._provider).__name__
        storage_path = self._storage.save_bytes(
            project_id, "renders", f"{project_id}.mp4", edit_result.video_bytes
        )
        asset_id = self._persist(project_id, storage_path, edit_result, render_engine, profile_name)
        return RenderResult(
            asset_id=asset_id,
            storage_path=storage_path,
            duration_sec=edit_result.duration_sec,
            resolution=edit_result.resolution,
            render_engine=render_engine,
        )

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
            music_bed_path=branding.music_bed_path,
            caption_style=branding.caption_style,
        )

    @staticmethod
    def _operation_for_entry(entry: TimelineEntry) -> RenderOperation:
        visual_clips = [
            VisualClipRef(
                storage_path=asset.storage_path,
                kind="video" if asset.shot_type in _VIDEO_SHOT_TYPES else "image",
                duration_sec=asset.duration_sec,
            )
            for asset in entry.visual_assets
            if asset.storage_path
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
            visual_clips=visual_clips,
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

    def _execute(self, plan: RenderPlan, profile_name: str) -> EditResult:
        spec = self._build_edit_spec(plan, profile_name)
        return self._provider.render(spec, on_progress=self._log_progress(plan.project_id))

    def _build_edit_spec(self, plan: RenderPlan, profile_name: str) -> EditSpec:
        subtitle_lines = [
            SubtitleLine(
                start_sec=line["start_sec"],
                end_sec=line["end_sec"],
                text=line["text"],
                emphasized=line["emphasized"],
            )
            for op in plan.operations
            for line in op.subtitle_lines
        ]
        return EditSpec(
            project_id=plan.project_id,
            segments=[self._edit_segment(op) for op in plan.operations],
            total_duration_sec=plan.total_duration_sec,
            subtitle_lines=subtitle_lines,
            caption_style=plan.caption_style,
            intro_asset=self._read_optional_branding_asset(plan.intro_asset_path),
            outro_asset=self._read_optional_branding_asset(plan.outro_asset_path),
            background_music=self._read_optional_branding_asset(plan.music_bed_path),
            profile_name=profile_name,
        )

    def _edit_segment(self, op: RenderOperation) -> EditSegment:
        return EditSegment(
            segment_id=op.segment_id,
            start_sec=op.start_sec,
            end_sec=op.end_sec,
            visual_clips=[
                VisualClip(
                    data=self._read_required_asset(
                        clip.storage_path, f"segment {op.segment_id} visual ({clip.kind})"
                    ),
                    kind=clip.kind,
                    duration_sec=clip.duration_sec,
                )
                for clip in op.visual_clips
            ],
            overlay_texts=op.overlay_texts,
            voice_audio=self._read_required_asset(
                op.voice_storage_path, f"segment {op.segment_id} narration"
            ),
            supplementary_audio=[
                self._read_required_asset(path, f"segment {op.segment_id} supplementary audio")
                for path in op.supplementary_audio_storage_paths
            ],
            transition_type=op.transition_type,
        )

    def _read_required_asset(self, storage_path: str, description: str) -> bytes:
        """Every asset here was already resolved by an earlier module
        (Asset Generation/Voice Generation) — a missing or unreadable
        file at this point is a genuine data-integrity problem, not
        something to skip past like an optional branding asset. Wrapped
        as `EditorInputError` (rather than a raw `FileNotFoundError`/
        `OSError` leaking out) so the failure is clearly attributed to
        *which* segment/asset before the whole job fails honestly.
        """
        try:
            return self._storage.read_bytes(storage_path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise EditorInputError(
                f"required asset missing or unreadable ({description}): {storage_path!r}: {exc}"
            ) from exc

    def _read_optional_branding_asset(self, storage_path: str | None) -> bytes | None:
        """Branding assets (intro/outro/music bed) are optional — `None`
        when a channel hasn't configured one (see `ChannelBranding`), and
        also skipped rather than failing the whole render if one is
        configured but no longer exists in storage: a missing accessory
        shouldn't abort an otherwise-successful video.
        """
        if not storage_path:
            return None
        if not self._storage.exists(storage_path):
            logger.warning("branding_asset_missing", storage_path=storage_path)
            return None
        return self._storage.read_bytes(storage_path)

    def _log_progress(self, project_id: str) -> ProgressCallback:
        def _on_progress(progress: RenderProgress) -> None:
            logger.info(
                "render_progress",
                project_id=project_id,
                stage=progress.stage,
                percent=round(progress.percent, 1),
                message=progress.message,
            )

        return _on_progress

    @staticmethod
    def _persist(
        project_id: str, storage_path: str, edit_result: EditResult, render_engine: str, profile_name: str
    ) -> str:
        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.VIDEO,
                provider=render_engine,
                storage_path=storage_path,
                duration_sec=edit_result.duration_sec,
                metadata_={"resolution": edit_result.resolution, "profile_name": profile_name},
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

            session.add(
                Render(
                    project_id=UUID(project_id),
                    asset_id=asset.id,
                    resolution=edit_result.resolution,
                    duration_sec=edit_result.duration_sec,
                    render_engine=render_engine,
                )
            )
        return asset_id
