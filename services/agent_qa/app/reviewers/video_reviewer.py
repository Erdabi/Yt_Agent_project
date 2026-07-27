"""VideoReviewer — missing scenes, missing assets, incorrect scene
order, bad transitions, visual consistency, rendering problems, and
video duration checks for a project's rendered video.

Missing assets/scene order/rendering problems (corrupt decode, black
frames)/duration-out-of-range are all genuinely deterministic — checked
against the real render file (ffprobe/ffmpeg, ../media_inspection.py)
and the persisted storyboard plan, no judgment call needed. Visual
consistency and transition/pacing appropriateness are inherently
qualitative, so those two go through one forced-tool-use call to the
configured `llm` provider (../llm_review.py) — text-only, describing the
planned shots/transitions/pacing, since no vision capability is wired
into this reviewer for actual frame content (see ThumbnailReviewer for
the one reviewer that does look at a real rendered image).
"""

from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.prompts import get_prompt_loader

from ..llm_review import run_llm_review
from ..media_inspection import decode_check, detect_black_frames
from ..qa_schema import Issue, ReviewInput, ReviewResult, SegmentData, build_review_result
from .base import Reviewer

logger = get_logger(__name__)

_CATEGORY = "video"
_PROMPT_PROVIDER = "claude"
#: A shot with no resolved asset is only ever expected for a render-time
#: text overlay (composited directly, no asset file) — anything else
#: missing an asset is a genuine gap.
_NO_ASSET_EXPECTED_SHOT_TYPES = {"text_overlay"}
#: How far the render's actual duration may drift from the script's
#: target/suggested length before it's flagged — a generous band since
#: neither figure is a hard production requirement.
_DURATION_TOLERANCE_RATIO = 0.25


class VideoReviewer(Reviewer):
    category = _CATEGORY

    def __init__(self) -> None:
        self._prompts = get_prompt_loader()
        self._prompt_version = get_settings().qa_prompt_version

    def review(self, review_input: ReviewInput) -> ReviewResult:
        deterministic_issues = self._deterministic_checks(review_input)
        llm_result = self._llm_check(review_input)
        return build_review_result(
            _CATEGORY, llm_result.summary, deterministic_issues + llm_result.issues
        )

    # --- Deterministic: assets, scene order, rendering, duration ------------

    def _deterministic_checks(self, review_input: ReviewInput) -> list[Issue]:
        issues: list[Issue] = []
        for segment in review_input.segments:
            issues.extend(self._segment_asset_issues(segment))
            issues.extend(self._segment_order_issues(segment))

        if review_input.render is None:
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail="No rendered video exists for this project.",
                    suggested_fix="Re-run the Video Agent's Rendering stage before publishing.",
                )
            )
            return issues

        issues.extend(self._rendering_problem_issues(review_input.render.local_path))
        issues.extend(self._duration_issues(review_input))
        return issues

    @staticmethod
    def _segment_asset_issues(segment: SegmentData) -> list[Issue]:
        if not segment.storyboard_shots:
            return [
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail=f"Segment {segment.order_index} ({segment.segment_type}) has no planned shots at all.",
                    suggested_fix="Re-run Asset Planning/Asset Generation for this segment.",
                )
            ]
        issues: list[Issue] = []
        for shot in segment.storyboard_shots:
            if shot.asset_id is None and shot.shot_type not in _NO_ASSET_EXPECTED_SHOT_TYPES:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="high",
                        detail=(
                            f"Segment {segment.order_index}'s {shot.shot_type} shot "
                            f"({shot.prompt_or_query!r}) never resolved to an asset."
                        ),
                        suggested_fix="Re-run Asset Generation for this segment's shot.",
                    )
                )
        return issues

    @staticmethod
    def _segment_order_issues(segment: SegmentData) -> list[Issue]:
        order_indices = [shot.order_index for shot in segment.storyboard_shots]
        if order_indices and sorted(order_indices) != list(range(len(order_indices))):
            return [
                Issue(
                    category=_CATEGORY,
                    severity="medium",
                    detail=(
                        f"Segment {segment.order_index}'s shots are not in a clean "
                        f"0..N-1 order: {order_indices}."
                    ),
                    suggested_fix="Re-plan this segment's shot order (Asset Planning).",
                )
            ]
        return []

    @staticmethod
    def _rendering_problem_issues(local_path: str) -> list[Issue]:
        issues: list[Issue] = []
        decode_error = decode_check(local_path)
        if decode_error:
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail=f"The render doesn't decode cleanly: {decode_error}",
                    suggested_fix="Re-render the video — the output file is corrupt.",
                )
            )
        black_spans = detect_black_frames(local_path)
        for start_sec, end_sec in black_spans:
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="medium",
                    detail=f"A black-frame span was detected from {start_sec:.1f}s to {end_sec:.1f}s.",
                    suggested_fix="Check the visual asset(s) covering that time range and re-render if needed.",
                )
            )
        return issues

    @staticmethod
    def _duration_issues(review_input: ReviewInput) -> list[Issue]:
        target = review_input.script_target_duration_sec or review_input.project_context.research.suggested_length_sec
        if not target or review_input.render is None or review_input.render.duration_sec is None:
            return []
        actual = review_input.render.duration_sec
        lower_bound = target * (1 - _DURATION_TOLERANCE_RATIO)
        upper_bound = target * (1 + _DURATION_TOLERANCE_RATIO)
        if lower_bound <= actual <= upper_bound:
            return []
        return [
            Issue(
                category=_CATEGORY,
                severity="medium",
                detail=(
                    f"The render is {actual:.1f}s long, outside the expected "
                    f"~{target}s target (±{_DURATION_TOLERANCE_RATIO:.0%})."
                ),
                suggested_fix="Adjust script length/pacing or the target duration, then re-render.",
            )
        ]

    # --- LLM: visual consistency + transition/pacing appropriateness -------

    def _llm_check(self, review_input: ReviewInput) -> ReviewResult:
        segments_context = [
            {
                "order_index": segment.order_index,
                "segment_type": segment.segment_type,
                "text": segment.text,
                "camera_framing": segment.production_metadata.camera_framing,
                "transition_type": segment.production_metadata.transition_type.value,
                "pacing": segment.production_metadata.pacing.value,
                "shots": [
                    {"shot_type": shot.shot_type, "prompt_or_query": shot.prompt_or_query}
                    for shot in segment.storyboard_shots
                ],
            }
            for segment in review_input.segments
        ]

        system_template = self._prompts.get(
            "qa", "video_review_system", version=self._prompt_version, provider=_PROMPT_PROVIDER
        )
        user_prompt = self._prompts.get(
            "qa", "video_review_user", version=self._prompt_version
        ).render(
            video_title=review_input.video_title,
            style_guide_summary=review_input.project_context.channel.style_guide_summary
            or "no style guide configured",
            segments=segments_context,
        )

        return run_llm_review(
            project_id=review_input.project_context.project.project_id,
            category=_CATEGORY,
            tool_name="report_video_issues",
            category_description="visual consistency and transition/pacing appropriateness",
            system_prompt=system_template.render(),
            user_prompt=user_prompt,
            prompt_name="video_review",
            prompt_version=system_template.version,
        )
