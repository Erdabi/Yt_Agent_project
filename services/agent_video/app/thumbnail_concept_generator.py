"""Claude-backed thumbnail concept generation for the Thumbnail Agent.

Turns a project's topic/title, full script, and channel branding into
several ranked, click-worthy thumbnail concepts — each with its own
concrete visual description, an image-generation prompt optimized for a
16:9 (1280x720) frame, a short on-thumbnail overlay text, and a rationale
for the audit trail. `ThumbnailAgent` (thumbnail_agent.py) then renders
the best-ranked concept(s) into real images via `get_provider("image_gen")`.

Both prompts sent to Claude are loaded at runtime from the Prompt
Management System (libs/prompts) — see
prompts/thumbnail/generate_concepts_system/ and
prompts/thumbnail/generate_concepts_user/ — following the same forced
tool-use, no-fallback pattern as
services/agent_research/app/idea_generator.py: a missing API key, a
failed call, a refusal, or a malformed response all raise
`ThumbnailConceptError` instead of returning a placeholder concept —
inventing a fake concept would defeat the entire purpose of this agent.

Deliberately a direct `anthropic` SDK call, not routed through
`libs.providers`/the Provider Registry: no agent in this codebase treats
Claude/LLM reasoning as a swappable `libs.providers` capability today
(Research's IdeaGenerator, the Script Agent's ScriptGenerator, and the
Manager's reasoning engine all call `anthropic.Anthropic` directly) — the
"reuse the Provider Registry" / "don't hardcode providers" requirements
this agent satisfies are about the *image generation* provider
(`get_provider("image_gen")` in thumbnail_agent.py), not the reasoning
model, matching the identical scoping every other agent's own LLM call
already uses.
"""

from dataclasses import dataclass
from typing import Any

import anthropic

from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader

logger = get_logger(__name__)

#: This engine only ever calls the Anthropic API, so it always asks for
#: the "claude" prompt variant if one exists — see
#: reasoning.py's `_PROMPT_PROVIDER` for the identical rationale.
_PROMPT_PROVIDER = "claude"


class ThumbnailConceptError(RuntimeError):
    """Concept generation could not produce a result. Never caught
    silently inside this module — always propagates so the caller's job
    fails honestly instead of shipping a fabricated/placeholder concept.
    """


@dataclass(frozen=True)
class GeneratedThumbnailConcept:
    concept_name: str
    visual_description: str
    image_prompt: str
    overlay_text: str
    rationale: str


_PROPOSE_CONCEPTS_TOOL = {
    "name": "propose_thumbnail_concepts",
    "description": "Propose one or more ranked YouTube thumbnail concepts for a finished video.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "concept_name": {
                            "type": "string",
                            "description": "A short, human-readable label for this concept.",
                        },
                        "visual_description": {
                            "type": "string",
                            "description": "What's depicted — composition, subject, action, mood.",
                        },
                        "image_prompt": {
                            "type": "string",
                            "description": (
                                "The optimized prompt to hand to an image generation provider "
                                "for a 16:9 (1280x720) frame — describe composition, subject, "
                                "action, mood, lighting, and color palette explicitly. Must not "
                                "ask the image model to render any text/letters/words."
                            ),
                        },
                        "overlay_text": {
                            "type": "string",
                            "description": (
                                "Short, punchy text to composite onto the thumbnail afterward "
                                "(a few words, not a sentence) — empty string if this concept "
                                "works better with no on-thumbnail text at all."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why this concept earns a click, for the audit trail.",
                        },
                    },
                    "required": [
                        "concept_name",
                        "visual_description",
                        "image_prompt",
                        "overlay_text",
                        "rationale",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["concepts"],
        "additionalProperties": False,
    },
}


class ThumbnailConceptGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._prompt_version = settings.thumbnail_prompt_version
        self._prompts = get_prompt_loader()
        self._client = (
            anthropic.Anthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )

    def generate(
        self,
        *,
        project_id: str,
        video_title: str,
        channel_niche: str,
        channel_persona: str,
        style_guide_summary: str,
        banned_topics: list[str] | None,
        target_audience: str | None,
        suggested_angle: str | None,
        emphasis_words: list[str] | None,
        script_text: str,
        concept_count: int,
    ) -> list[GeneratedThumbnailConcept]:
        if self._client is None:
            raise ThumbnailConceptError("ANTHROPIC_API_KEY is not configured")

        try:
            system_template = self._prompts.get(
                "thumbnail", "generate_concepts_system",
                version=self._prompt_version, provider=_PROMPT_PROVIDER,
            )
            system_prompt = system_template.render()
            user_prompt = self._prompts.get(
                "thumbnail", "generate_concepts_user", version=self._prompt_version,
            ).render(
                video_title=video_title,
                channel_niche=channel_niche,
                channel_persona=channel_persona,
                style_guide_summary=style_guide_summary,
                banned_topics=banned_topics,
                target_audience=target_audience,
                suggested_angle=suggested_angle,
                emphasis_words=emphasis_words,
                script_text=script_text,
                concept_count=concept_count,
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise ThumbnailConceptError(f"prompt template error: {exc}") from exc

        try:
            with track_llm_call(
                project_id=project_id,
                agent_name="thumbnail",
                call_site="thumbnail_concept_generator.generate",
                model=self._model,
                prompt_name="generate_concepts",
                prompt_version=system_template.version,
            ) as usage:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    output_config={"effort": self._effort},
                    system=system_prompt,
                    tools=[_PROPOSE_CONCEPTS_TOOL],
                    tool_choice={"type": "tool", "name": "propose_thumbnail_concepts"},
                    messages=[{"role": "user", "content": user_prompt}],
                )
                usage["input_tokens"] = response.usage.input_tokens
                usage["output_tokens"] = response.usage.output_tokens
        except anthropic.APIError as exc:
            logger.error("thumbnail_concept_generator_call_failed", error=str(exc))
            raise ThumbnailConceptError(f"Claude API call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            logger.warning("thumbnail_concept_generator_refusal")
            raise ThumbnailConceptError("Claude declined to respond")

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            logger.error("thumbnail_concept_generator_no_tool_use", stop_reason=response.stop_reason)
            raise ThumbnailConceptError(
                f"Claude did not return any concepts (stop_reason={response.stop_reason})"
            )

        concepts_raw: list[dict[str, Any]] = tool_use.input["concepts"]
        if not concepts_raw:
            raise ThumbnailConceptError("Claude returned an empty concepts list")

        return [GeneratedThumbnailConcept(**concept) for concept in concepts_raw]
