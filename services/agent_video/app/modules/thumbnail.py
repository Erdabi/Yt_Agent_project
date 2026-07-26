"""Thumbnail Generation module.

Runs last inside `VideoAgent.run()`, alongside — not part of — the six
Asset Planning -> Rendering pipeline (see video_agent.py and
pipeline_schema.py): it has no real ordering dependency on that pipeline
at all, since it only needs the approved script/title and the channel's
style guide, not anything Rendering produces. It runs after it anyway to
keep `VideoAgent.run()` a single simple sequence rather than introducing
concurrency inside one job for a modest latency win.

    Input:  JobContext
    Output: dict (asset id, storage path, variant label)

Builds an image-gen prompt from `prompts/video/thumbnail_prompt/` (the
video's title, the channel's style guide, and the hook segment's
emphasis words), checks the Asset Cache before calling
`libs.providers.get_provider("image_gen")` for the base image — the
*only* capability this module calls, same as every other provider-aware
module in this pipeline — then composites the video's title onto that
image locally (Pillow; not a provider capability, the same way ffmpeg
compositing in Rendering isn't one either) and persists it. Only one
variant is produced today; automated thumbnail/title A/B variant testing
is a later phase (docs/architecture/06-roadmap.md, Phase 4), so this
module always marks its one output `is_selected=True`.
"""

import io
from typing import Any
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Thumbnail
from libs.models.channel import Channel
from libs.models.enums import AssetType as PhysicalAssetType
from libs.models.enums import ScriptSegmentType
from libs.models.idea import VideoIdea
from libs.models.project import Project
from libs.models.script import Script, ScriptSegment
from libs.prompts.registry import get_prompt_loader
from libs.providers.registry import get_provider
from libs.schemas.jobs import JobContext
from libs.schemas.script_production import SegmentProductionMetadata
from libs.storage import get_storage_backend

from ..asset_cache import AssetCache, AssetCacheKey

logger = get_logger(__name__)

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEFAULT_STYLE_GUIDE_SUMMARY = "clean, bold, high-contrast composition with no visual clutter"
_VARIANT_LABEL = "v1"


class ThumbnailModule:
    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._cache = AssetCache()

    def generate(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "Thumbnail Generation requires a project_id — it is always "
                "dispatched against an existing project"
            )

        video_title, style_guide_summary, emphasis_words = self._load_input(context.project_id)

        prompt_template = get_prompt_loader().get("video", "thumbnail_prompt")
        rendered_prompt = prompt_template.render(
            video_title=video_title,
            style_guide_summary=style_guide_summary,
            emphasis_words=emphasis_words,
        )

        base_image, provider_name = self._resolve_base_image(rendered_prompt)
        final_image = self._composite_title(base_image, video_title)

        storage_path = self._storage.save_bytes(
            context.project_id, "thumbnail", f"{_VARIANT_LABEL}.png", final_image
        )
        asset_id = self._persist(context.project_id, storage_path, provider_name)

        logger.info(
            "thumbnail_generation_complete",
            project_id=context.project_id,
            asset_id=asset_id,
            storage_path=storage_path,
        )
        return {"asset_id": asset_id, "storage_path": storage_path, "variant_label": _VARIANT_LABEL}

    def _resolve_base_image(self, rendered_prompt: str) -> tuple[bytes, str]:
        provider = get_provider("image_gen")
        provider_name = type(provider).__name__
        cache_key = AssetCacheKey(
            capability="image_gen",
            provider_name=provider_name,
            asset_type="thumbnail",
            prompt=rendered_prompt,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._storage.read_bytes(cached.storage_path), cached.provider_name

        data = provider.generate(rendered_prompt)
        self._cache.put(
            cache_key,
            data=data,
            extension="png",
            physical_asset_type=PhysicalAssetType.IMAGE,
            provider_name=provider_name,
        )
        return data, provider_name

    @staticmethod
    def _composite_title(base_image_bytes: bytes, title: str) -> bytes:
        image = Image.open(io.BytesIO(base_image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size

        font_size = max(28, width // 14)
        font = ImageFont.truetype(_FONT_PATH, font_size)
        wrapped = ThumbnailModule._wrap_to_width(draw, title.upper(), font, max_width=width * 0.92)

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

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

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
    def _persist(project_id: str, storage_path: str, provider_name: str) -> str:
        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.IMAGE,
                provider=provider_name,
                storage_path=storage_path,
                metadata_={"variant_label": _VARIANT_LABEL},
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

            session.add(
                Thumbnail(
                    project_id=UUID(project_id),
                    asset_id=asset.id,
                    variant_label=_VARIANT_LABEL,
                    is_selected=True,
                )
            )
        return asset_id

    @staticmethod
    def _load_input(project_id: str) -> tuple[str, str, list[str]]:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            if project is None:
                raise LookupError(f"project {project_id} not found")
            channel = session.get(Channel, project.channel_id)
            if channel is None:
                raise LookupError(f"channel {project.channel_id} not found")
            idea = session.get(VideoIdea, project.idea_id)
            if idea is None:
                raise LookupError(f"video idea {project.idea_id} not found")

            # Scripts are versioned, never mutated in place — the latest
            # version is always the one to build a thumbnail from, same
            # as video_agent.py's own `_load_input`.
            script = session.scalar(
                select(Script)
                .where(Script.project_id == project.id)
                .order_by(Script.version.desc())
                .limit(1)
            )
            if script is None:
                raise LookupError(f"no script found for project {project_id}")

            hook_segment = session.scalar(
                select(ScriptSegment)
                .where(
                    ScriptSegment.script_id == script.id,
                    ScriptSegment.segment_type == ScriptSegmentType.HOOK,
                )
                .order_by(ScriptSegment.order_index)
                .limit(1)
            )
            emphasis_words: list[str] = []
            if hook_segment is not None:
                metadata = SegmentProductionMetadata.model_validate(hook_segment.production_metadata)
                emphasis_words = metadata.emphasis_words

            persona = channel.persona_config or {}
            style_guide_summary = persona.get("style_guide_summary") or _DEFAULT_STYLE_GUIDE_SUMMARY

        return idea.title, style_guide_summary, emphasis_words
