"""ThumbnailReviewer — readability, title/text visibility, branding
consistency, and click-potential checks for a project's rendered
thumbnail.

The only reviewer that sends the actual rendered image (not just its
generation metadata) to the configured `llm` provider — genuinely
looking at the image is the only honest way to judge whether it's
legible/eye-catching, unlike VideoReviewer's shot-*description*-only
judgment (no video-frame vision capability is wired in there).
"""

from libs.core.config import get_settings
from libs.prompts import get_prompt_loader

from ..llm_review import run_llm_review
from ..qa_schema import Issue, ReviewInput, ReviewResult, build_review_result
from .base import Reviewer

_CATEGORY = "thumbnail"
_PROMPT_PROVIDER = "claude"


class ThumbnailReviewer(Reviewer):
    category = _CATEGORY

    def __init__(self) -> None:
        self._prompts = get_prompt_loader()
        self._prompt_version = get_settings().qa_prompt_version

    def review(self, review_input: ReviewInput) -> ReviewResult:
        if review_input.thumbnail is None:
            return build_review_result(
                _CATEGORY,
                "No thumbnail exists for this project.",
                [
                    Issue(
                        category=_CATEGORY,
                        severity="high",
                        detail="No thumbnail has been generated for this project.",
                        suggested_fix="Run the Thumbnail Agent before publishing.",
                    )
                ],
            )
        return self._llm_check(review_input)

    def _llm_check(self, review_input: ReviewInput) -> ReviewResult:
        thumbnail = review_input.thumbnail
        system_template = self._prompts.get(
            "qa", "thumbnail_review_system", version=self._prompt_version, provider=_PROMPT_PROVIDER
        )
        user_prompt = self._prompts.get(
            "qa", "thumbnail_review_user", version=self._prompt_version
        ).render(
            video_title=review_input.video_title,
            style_guide_summary=review_input.project_context.channel.style_guide_summary
            or "no style guide configured",
            concept_name=thumbnail.concept_name,
            overlay_text=thumbnail.overlay_text,
            image_prompt=thumbnail.prompt,
        )

        return run_llm_review(
            project_id=review_input.project_context.project.project_id,
            category=_CATEGORY,
            tool_name="report_thumbnail_issues",
            category_description=(
                "thumbnail readability, title visibility, branding consistency, and click potential"
            ),
            system_prompt=system_template.render(),
            user_prompt=user_prompt,
            prompt_name="thumbnail_review",
            prompt_version=system_template.version,
            images=[thumbnail.image_bytes],
        )
