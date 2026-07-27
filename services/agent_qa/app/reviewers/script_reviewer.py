"""ScriptReviewer — factual consistency, completeness, quality,
engagement, repetition, grammar, and alignment-with-research checks for
a project's finished script.

Two checks are genuinely deterministic and don't need an LLM's judgment:
completeness (does the script have the structural beats a video needs at
all?) and exact-duplicate repetition (the same line appearing twice,
word for word). Everything else this reviewer is asked to check —
factual consistency, prose quality, engagement, non-exact repetition
(overused phrasing), grammar, and whether the script actually uses the
research it was given — is inherently a judgment call, made via one
forced-tool-use call to the configured `llm` provider (../llm_review.py).
"""

from libs.core.config import get_settings
from libs.prompts import get_prompt_loader

from ..llm_review import run_llm_review
from ..qa_schema import Issue, ReviewInput, ReviewResult, build_review_result
from .base import Reviewer

_CATEGORY = "script"
#: This reviewer only ever calls the Anthropic-backed `llm` provider by
#: default, so it always asks for the "claude" prompt variant if one
#: exists — same rationale as every other forced-tool-use call site in
#: this codebase (e.g. services/agent_research/app/idea_generator.py).
_PROMPT_PROVIDER = "claude"
#: A completed script needs at least one opening beat and one closing
#: beat — anything else (introduction, how many main sections) is a
#: legitimate creative choice this reviewer doesn't second-guess
#: mechanically.
_REQUIRED_OPENING_TYPES = {"hook"}
_REQUIRED_CLOSING_TYPES = {"ending", "call_to_action"}


class ScriptReviewer(Reviewer):
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

    # --- Deterministic: completeness + exact-duplicate repetition ----------

    def _deterministic_checks(self, review_input: ReviewInput) -> list[Issue]:
        issues: list[Issue] = []
        segment_types = {segment.segment_type for segment in review_input.segments}

        if not (segment_types & _REQUIRED_OPENING_TYPES):
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail="The script has no hook segment.",
                    suggested_fix="Add an opening hook segment before the video is published.",
                )
            )
        if not (segment_types & _REQUIRED_CLOSING_TYPES):
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="medium",
                    detail="The script has no ending or call-to-action segment.",
                    suggested_fix=(
                        "Add a closing segment (ending or call to action) so the video "
                        "has a real conclusion."
                    ),
                )
            )

        seen_by_normalized_text: dict[str, int] = {}
        for segment in review_input.segments:
            normalized = " ".join(segment.text.split()).lower()
            if not normalized:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="high",
                        detail=f"Segment {segment.order_index} ({segment.segment_type}) has no text.",
                        suggested_fix="Write narration text for this segment before publishing.",
                    )
                )
                continue
            earlier_order_index = seen_by_normalized_text.get(normalized)
            if earlier_order_index is not None:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="medium",
                        detail=(
                            f"Segment {segment.order_index} repeats segment "
                            f"{earlier_order_index} almost word-for-word: "
                            f'"{segment.text[:80]}"'
                        ),
                        suggested_fix=(
                            "Rewrite one of these segments so it adds something new "
                            "instead of repeating the other."
                        ),
                    )
                )
            else:
                seen_by_normalized_text[normalized] = segment.order_index
        return issues

    # --- LLM: factual consistency, quality, engagement, grammar, research --

    def _llm_check(self, review_input: ReviewInput) -> ReviewResult:
        research = review_input.project_context.research
        knowledge_package = review_input.project_context.knowledge_package

        system_template = self._prompts.get(
            "qa", "script_review_system", version=self._prompt_version, provider=_PROMPT_PROVIDER
        )
        user_prompt = self._prompts.get(
            "qa", "script_review_user", version=self._prompt_version
        ).render(
            video_title=review_input.video_title,
            channel_niche=review_input.project_context.channel.niche or "general",
            channel_persona=review_input.project_context.channel.persona or "general audience",
            target_audience=research.target_audience,
            suggested_angle=research.suggested_angle,
            research_notes=research.research_notes,
            knowledge_package_summary=knowledge_package.summary if knowledge_package else None,
            verified_facts=(
                [fact.statement for fact in knowledge_package.verified_facts]
                if knowledge_package else None
            ),
            script_content=review_input.script_content,
        )

        return run_llm_review(
            project_id=review_input.project_context.project.project_id,
            category=_CATEGORY,
            tool_name="report_script_issues",
            category_description="script quality, factual consistency, and engagement",
            system_prompt=system_template.render(),
            user_prompt=user_prompt,
            prompt_name="script_review",
            prompt_version=system_template.version,
        )
