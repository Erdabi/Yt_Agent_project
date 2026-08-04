"""`VideoAgent` — the single agent the Manager dispatches for the whole
`video_creation` stage.

Internally, video production is a pipeline of seven modules, run in
sequence inside one `run()` call, so one `Job` row and one Manager
decision still covers all of it:

    Voice Generation -> Visual Beat Planning -> Asset Planning
        -> Asset Generation -> Subtitle Generation
        -> Timeline Building -> Rendering

Plus the Thumbnail Agent (thumbnail_agent.py), which runs alongside but
has no place in that dependency chain — it only needs the finished
script and channel branding, not anything Rendering produces. It is a
genuine, independent `BaseAgent` in its own right (own concept-generation
LLM call, own Celery task for standalone regeneration — see its own
module docstring for why), but is invoked here *in-process* rather than
as its own Manager-dispatched stage, so this job still reports to the
Manager exactly once for the whole `video_creation` stage. Each module
has one clearly typed input and one clearly typed output
(services/agent_video/app/pipeline_schema.py) — no module reaches into
another's internals, and each module calls at most the one
`libs.providers` capability it genuinely needs: Asset Generation calls
`image_gen`/`video_gen`/`stock_media`/`audio_library`, Voice Generation
calls `tts`, and Rendering calls `editor` (its compositor). That is what
makes a provider swappable independently: changing which class backs
`tts` in config/providers.yaml only touches Voice Generation's own call
site, because Timeline Building never sees a provider at all, only the
`VoiceSegment`/`ResolvedAsset` values Voice Generation/Asset Generation
already resolved. The Thumbnail Agent also calls `image_gen` on its own
(see thumbnail_agent.py) — independent of Asset Generation's own use of
the same capability, since a thumbnail's image has nothing to do with any
one storyboard shot.

The real dependency chain is: Visual Beat Planning needs Voice
Generation's *measured* narration durations (it sizes each segment's
visuals from how long that segment actually takes to say — see that
module's docstring); Asset Planning needs those beats; Asset Generation
needs Asset Planning's plan; Subtitle Generation needs Voice
Generation's durations/timing; Timeline Building needs Asset Generation,
Voice Generation, and Subtitle Generation; Rendering needs Timeline
Building.

Voice Generation therefore runs *first*, ahead of anything visual. That
ordering is load-bearing rather than incidental: it is what lets the
number and length of a segment's visuals be derived from real synthesized
audio instead of from the Script Agent's pre-production
`estimated_speech_wpm` guess, which is made before any audio exists and
routinely diverges from the TTS provider's actual speaking pace.

The trade-off that consolidation makes explicit: a failure partway
through (e.g. Rendering breaks) means the *whole* video job is retried by
the Manager, including every already-succeeded module's work, not just
the broken one. That's an acceptable cost here — most of these modules
are fast/cheap compared to Rendering, and a partial-video retry was never
really "resume where it broke" anyway, since Timeline Building's output
depends on all of them regardless.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from libs.agents.base import BaseAgent
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.channel import Channel
from libs.models.project import Project
from libs.models.script import Script, ScriptSegment
from libs.schemas.jobs import JobContext
from libs.schemas.script_production import SegmentProductionMetadata

from .modules.asset_generation import AssetGenerationModule
from .modules.asset_planning import AssetPlanningModule
from .modules.rendering import RenderingModule
from .modules.subtitle_generation import SubtitleGenerationModule
from .modules.timeline_building import TimelineBuildingModule
from .modules.visual_beat_planning import VisualBeatPlanningModule
from .modules.voice_generation import VoiceGenerationModule
from .pipeline_schema import ChannelBranding, SegmentInput
from .thumbnail_agent import ThumbnailAgent

logger = get_logger(__name__)


class VideoAgent(BaseAgent):
    name = "video"

    def __init__(self) -> None:
        self._visual_beat_planning = VisualBeatPlanningModule()
        self._asset_planning = AssetPlanningModule()
        self._asset_generation = AssetGenerationModule()
        self._voice_generation = VoiceGenerationModule()
        self._subtitle_generation = SubtitleGenerationModule()
        self._timeline_building = TimelineBuildingModule()
        self._rendering = RenderingModule()
        self._thumbnail = ThumbnailAgent()

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Video Agent requires a project_id — it is always dispatched "
                "against an existing project by the Manager Agent's workflow plan"
            )

        segments, branding = self._load_input(context.project_id)

        voice_segments = self._voice_generation.synthesize(context.project_id, segments)
        visual_beats = self._visual_beat_planning.plan(segments, voice_segments)
        planned_assets = self._asset_planning.plan(segments, visual_beats)
        resolved_assets = self._asset_generation.generate(context.project_id, planned_assets)
        subtitle_cues = self._subtitle_generation.generate(segments, voice_segments)
        timeline = self._timeline_building.build(
            context.project_id, segments, resolved_assets, voice_segments, subtitle_cues
        )
        render_result = self._rendering.render(context.project_id, timeline, branding)
        # `.run()` directly (not `.execute_job()`): this is an in-process
        # call within the Video Agent's own job, not a separate
        # Manager-tracked job — see thumbnail_agent.py's module docstring.
        thumbnail_result = self._thumbnail.run(context)

        return {
            "segment_count": len(segments),
            "visual_beat_count": len(visual_beats),
            "planned_asset_count": len(planned_assets),
            "resolved_asset_count": len(resolved_assets),
            "subtitle_cue_count": len(subtitle_cues),
            "total_duration_sec": timeline.total_duration_sec,
            "render": {
                "asset_id": render_result.asset_id,
                "storage_path": render_result.storage_path,
                "duration_sec": render_result.duration_sec,
            },
            "thumbnail": thumbnail_result,
        }

    @staticmethod
    def _load_input(project_id: str) -> tuple[list[SegmentInput], ChannelBranding]:
        """Everything this pipeline needs about the project, loaded once.

        Deliberately *not* `libs.context.build_project_context` — that
        builder is scoped to what the Script Agent needs before a script
        exists (research summary, knowledge package, a prompt version to
        resolve). The Video Agent needs the opposite: the script that
        already exists, plus channel branding, and nothing about research
        or prompts at all, since this pipeline makes no LLM calls.
        """
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            if project is None:
                raise LookupError(f"project {project_id} not found")
            channel = session.get(Channel, project.channel_id)
            if channel is None:
                raise LookupError(f"channel {project.channel_id} not found")

            # Scripts are versioned, never mutated in place
            # (libs/models/script.py) — the latest version is always the
            # one to produce video from.
            script = session.scalar(
                select(Script)
                .where(Script.project_id == project.id)
                .order_by(Script.version.desc())
                .limit(1)
            )
            if script is None:
                raise LookupError(f"no script found for project {project_id}")

            segment_rows = session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script.id)
                .order_by(ScriptSegment.order_index)
            ).all()
            if not segment_rows:
                raise LookupError(f"script {script.id} has no segments")

            segments = [
                SegmentInput(
                    segment_id=str(row.id),
                    order_index=row.order_index,
                    segment_type=row.segment_type.value,
                    text=row.text,
                    scene_notes=row.scene_notes,
                    visual_notes=row.visual_notes,
                    production=SegmentProductionMetadata.model_validate(row.production_metadata),
                )
                for row in segment_rows
            ]

            persona = channel.persona_config or {}
            branding = ChannelBranding(
                intro_asset_path=persona.get("intro_asset_path"),
                outro_asset_path=persona.get("outro_asset_path"),
                caption_style=persona.get("caption_style"),
                music_bed_description=persona.get("music_bed_description"),
                music_bed_path=persona.get("music_bed_path"),
            )

        return segments, branding
