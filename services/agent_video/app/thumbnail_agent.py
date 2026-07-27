"""The Thumbnail Agent.

Generates a YouTube thumbnail for a project whose script already exists —
analyzing the project's topic, title, full script, and channel branding,
proposing one or more ranked thumbnail concepts, turning the best-ranked
concept(s) into an optimized image-generation prompt, rendering each at
exactly 1280x720 through whichever `image_gen` provider is configured,
and persisting the result as an `Asset`/`Thumbnail` row pair with a rich,
self-contained metadata record.

    Input:  JobContext (project_id)
    Output: dict (selected variant's asset id/storage path, plus every
            rendered variant)

Architecture — reuse, not reinvention:
- **Provider Registry** (`libs.providers.get_provider("image_gen")`) — the
  *only* capability this agent calls for image generation; which class
  backs it is a `config/providers.yaml` edit, never a code change here.
  Concept *reasoning* (the Claude call in thumbnail_concept_generator.py)
  is a direct `anthropic` SDK call instead, matching every other agent's
  own LLM-reasoning step in this codebase (Research/Script/Manager) —
  see that module's docstring for why that's the correct scope for "don't
  hardcode providers" rather than an inconsistency.
- **Asset Cache** (`.asset_cache.AssetCache`) — the same cache Asset
  Generation/Voice Generation/the old Thumbnail Generation module already
  shared; a concept whose exact `image_prompt` was already rendered
  (by this project or any other) is never re-generated.
- **Prompt Management** (`libs.prompts`) — the concept-generation prompts
  live at prompts/thumbnail/generate_concepts_{system,user}/, versioned
  and pinnable via `THUMBNAIL_PROMPT_VERSION`, not embedded as Python
  string constants.
- **Project Context** (`libs.context.build_project_context`) — supplies
  the channel profile (niche, persona, banned topics, style guide) and
  research summary (topic/title, target audience, suggested angle)
  without this agent re-querying `Channel`/`VideoIdea` itself. The one
  thing `ProjectContext` deliberately does *not* carry is script content
  (see libs/context/schema.py's module docstring — it predates any
  script existing for most of its other consumers), so this agent reads
  the `Script`/`ScriptSegment` rows directly, the same way the module
  this replaces always did.

Deployment — a standalone Agent, invoked in-process:
This is a genuine, independent `BaseAgent` — reusable on its own (its own
Celery task, `agents.thumbnail.run`, registered in worker.py, for
regenerating a thumbnail without rerunning the whole video pipeline) —
but its code stays inside `services/agent_video/app/` and it is called
*in-process* from `VideoAgent.run()` (thumbnail-worthy inputs — a
finished script and Rendering's completed video — already exist by that
point in the same job) rather than becoming its own `ProjectStage`/
Manager-dispatched step. The Manager's workflow plan is deliberately
fixed at five stages (services/orchestrator/app/manager/workflow.py) —
promoting this to a sixth would mean the Manager tracking two jobs (video
+ thumbnail) per `video_creation` stage, a materially bigger change to
the workflow/dispatcher than anything this task asked for. `VideoAgent`
still reports exactly one job to the Manager for the whole stage, same
as before.

`reports_to_manager = False`: a standalone regeneration job's project may
be in *any* stage (or already `PUBLISHED`) — `ManagerAgent.handle_job_finished`
(services/orchestrator/app/manager/manager.py) makes its advance/retry/
escalate decision from the project's *current* stage, not the reporting
job's `agent_name`, so letting a stray thumbnail job notify the Manager
could incorrectly advance/retry/escalate whatever stage that project
actually happens to be in. Same reasoning as the Analytics Agent
(libs/agents/base.py's own docstring).
"""

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from libs.agents.base import BaseAgent
from libs.context import ProjectContext, build_project_context
from libs.core.config import get_settings
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Thumbnail
from libs.models.enums import AssetType as PhysicalAssetType
from libs.models.enums import ScriptSegmentType
from libs.models.script import Script, ScriptSegment
from libs.providers.registry import get_provider
from libs.schemas.jobs import JobContext
from libs.schemas.script_production import SegmentProductionMetadata
from libs.storage import get_storage_backend

from .asset_cache import AssetCache, AssetCacheKey
from .thumbnail_concept_generator import GeneratedThumbnailConcept, ThumbnailConceptGenerator

logger = get_logger(__name__)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEFAULT_STYLE_GUIDE_SUMMARY = "clean, bold, high-contrast composition with no visual clutter"
#: YouTube's standard thumbnail resolution — enforced locally (crop-to-
#: cover, then resize) regardless of whatever native size/aspect ratio
#: the configured image_gen provider actually returns.
_TARGET_WIDTH = 1280
_TARGET_HEIGHT = 720


@dataclass(frozen=True)
class _ScriptInput:
    script_text: str
    emphasis_words: list[str]


class ThumbnailAgent(BaseAgent):
    name = "thumbnail"
    reports_to_manager = False

    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._cache = AssetCache()
        self._concepts = ThumbnailConceptGenerator()

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Thumbnail Agent requires a project_id — it is always "
                "dispatched (or invoked in-process) against an existing project"
            )

        project_context = build_project_context(
            context.project_id, consumer_prompt=("thumbnail", "generate_concepts_system")
        )
        # `ProjectContext.research.topic` is the video's title
        # (`VideoIdea.title`, see libs/context/builder.py); script content
        # itself isn't part of `ProjectContext` (see that module's
        # docstring), so it's read directly here.
        video_title = project_context.research.topic
        script_input = self._load_script(context.project_id)

        settings = get_settings()
        concepts = self._concepts.generate(
            project_id=context.project_id,
            video_title=video_title,
            channel_niche=project_context.channel.niche or "general",
            channel_persona=project_context.channel.persona or "general audience",
            style_guide_summary=project_context.channel.style_guide_summary
            or _DEFAULT_STYLE_GUIDE_SUMMARY,
            banned_topics=project_context.channel.banned_topics,
            target_audience=project_context.research.target_audience,
            suggested_angle=project_context.research.suggested_angle,
            emphasis_words=script_input.emphasis_words,
            script_text=script_input.script_text,
            concept_count=settings.thumbnail_concept_count,
        )

        render_count = max(1, min(settings.thumbnail_render_count, len(concepts)))
        variants = [
            self._render_variant(
                context.project_id, rank=rank, concept=concept,
                video_title=video_title, project_context=project_context,
                concept_count_requested=settings.thumbnail_concept_count,
                render_count_requested=settings.thumbnail_render_count,
            )
            for rank, concept in enumerate(concepts[:render_count], start=1)
        ]

        logger.info(
            "thumbnail_generation_complete",
            project_id=context.project_id,
            variant_count=len(variants),
            selected_asset_id=variants[0]["asset_id"],
        )
        return {**variants[0], "variant_count": len(variants), "variants": variants}

    # --- Rendering one concept into a stored asset --------------------------

    def _render_variant(
        self,
        project_id: str,
        *,
        rank: int,
        concept: GeneratedThumbnailConcept,
        video_title: str,
        project_context: ProjectContext,
        concept_count_requested: int,
        render_count_requested: int,
    ) -> dict[str, Any]:
        image_bytes, provider_name = self._resolve_image(concept.image_prompt)
        final_bytes = self._finalize_image(image_bytes, concept.overlay_text or video_title)

        variant_label = f"v{rank}"
        storage_path = self._storage.save_bytes(
            project_id, "thumbnail", f"{variant_label}.png", final_bytes
        )
        generated_at = datetime.now(UTC)
        metadata = {
            "prompt": concept.image_prompt,
            "provider": provider_name,
            # The image_gen provider interface (libs/providers/image_gen/
            # base.py) doesn't report back which underlying model it used
            # — unlike TTS's SynthesisResult, no provider today surfaces
            # one — so this is honestly `None` rather than guessed.
            "model": None,
            "generation_settings": {
                "resolution": f"{_TARGET_WIDTH}x{_TARGET_HEIGHT}",
                "concept_generation_model": project_context.manager.anthropic_model,
                "concept_generation_effort": project_context.manager.anthropic_effort,
                "concept_count_requested": concept_count_requested,
                "render_count_requested": render_count_requested,
            },
            "concept_name": concept.concept_name,
            "visual_description": concept.visual_description,
            "overlay_text": concept.overlay_text,
            "rationale": concept.rationale,
            "variation": rank,
            "generated_at": generated_at.isoformat(),
        }
        asset_id = self._persist(project_id, storage_path, provider_name, metadata, variant_label, rank)

        return {
            "asset_id": asset_id,
            "storage_path": storage_path,
            "variant_label": variant_label,
            "concept_name": concept.concept_name,
            "is_selected": rank == 1,
        }

    def _resolve_image(self, image_prompt: str) -> tuple[bytes, str]:
        provider = get_provider("image_gen")
        provider_name = type(provider).__name__
        cache_key = AssetCacheKey(
            capability="image_gen",
            provider_name=provider_name,
            asset_type="thumbnail",
            prompt=image_prompt,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._storage.read_bytes(cached.storage_path), cached.provider_name

        data = provider.generate(image_prompt)
        self._cache.put(
            cache_key,
            data=data,
            extension="png",
            physical_asset_type=PhysicalAssetType.IMAGE,
            provider_name=provider_name,
        )
        return data, provider_name

    @staticmethod
    def _finalize_image(image_bytes: bytes, overlay_text: str) -> bytes:
        """Enforce exactly 1280x720 (crop-to-cover, never letterboxed —
        a thumbnail should fill the frame, unlike video content where a
        pad/letterbox is the safer default), composite `overlay_text` if
        given, and always emit PNG regardless of the provider's native
        format (Pillow decodes whatever it returned).
        """
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image = ThumbnailAgent._crop_to_cover(image, _TARGET_WIDTH, _TARGET_HEIGHT)
        if overlay_text:
            image = ThumbnailAgent._composite_overlay_text(image, overlay_text)

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _crop_to_cover(image: Image.Image, target_width: int, target_height: int) -> Image.Image:
        target_ratio = target_width / target_height
        width, height = image.size
        current_ratio = width / height
        if current_ratio > target_ratio:
            new_width = round(height * target_ratio)
            left = (width - new_width) // 2
            image = image.crop((left, 0, left + new_width, height))
        elif current_ratio < target_ratio:
            new_height = round(width / target_ratio)
            top = (height - new_height) // 2
            image = image.crop((0, top, width, top + new_height))
        return image.resize((target_width, target_height), Image.LANCZOS)

    @staticmethod
    def _composite_overlay_text(image: Image.Image, text: str) -> Image.Image:
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size

        font_size = max(28, width // 14)
        font = ImageFont.truetype(_FONT_PATH, font_size)
        wrapped = ThumbnailAgent._wrap_to_width(draw, text.upper(), font, max_width=width * 0.92)

        text_bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        padding = 24
        box_top = max(0, height - text_height - padding * 3)
        draw.rectangle([0, box_top, width, height], fill=(0, 0, 0, 160))

        text_x = (width - text_width) / 2
        text_y = box_top + padding
        draw.multiline_text(
            (text_x, text_y), wrapped, font=font, fill="white", align="center",
            stroke_width=max(2, font_size // 16), stroke_fill="black",
        )
        return image

    @staticmethod
    def _wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
        """Wraps by actual measured glyph width rather than a guessed
        character count, so a line never overflows `max_width` regardless
        of image size or font metrics.
        """
        words = text.split()
        if not words:
            return ""
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return "\n".join(lines)

    @staticmethod
    def _persist(
        project_id: str,
        storage_path: str,
        provider_name: str,
        metadata: dict[str, Any],
        variant_label: str,
        rank: int,
    ) -> str:
        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.IMAGE,
                provider=provider_name,
                storage_path=storage_path,
                metadata_=metadata,
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

            session.add(
                Thumbnail(
                    project_id=UUID(project_id),
                    asset_id=asset.id,
                    variant_label=variant_label,
                    is_selected=rank == 1,
                )
            )
        return asset_id

    # --- Script loading (ProjectContext deliberately carries no script) ----

    @staticmethod
    def _load_script(project_id: str) -> _ScriptInput:
        with sync_session_scope() as session:
            script = session.scalar(
                select(Script)
                .where(Script.project_id == UUID(project_id))
                .order_by(Script.version.desc())
                .limit(1)
            )
            if script is None:
                raise LookupError(f"no script found for project {project_id}")

            segments = session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script.id)
                .order_by(ScriptSegment.order_index)
            ).all()
            if not segments:
                raise LookupError(f"script {script.id} has no segments")

            script_text = "\n\n".join(
                f"[{segment.segment_type.value}] {segment.text}" for segment in segments
            )

            hook_segment = next(
                (s for s in segments if s.segment_type == ScriptSegmentType.HOOK), None
            )
            emphasis_words: list[str] = []
            if hook_segment is not None:
                metadata = SegmentProductionMetadata.model_validate(hook_segment.production_metadata)
                emphasis_words = metadata.emphasis_words

        return _ScriptInput(script_text=script_text, emphasis_words=emphasis_words)
