"""LLM-backed idea generation for the Research Agent.

Both prompts sent to the model are loaded at runtime from the Prompt
Management System (libs/prompts) rather than embedded as Python string
constants — see prompts/research/generate_ideas_system/ and
prompts/research/generate_ideas_user/, and
services/orchestrator/app/manager/reasoning.py for the same pattern
applied to the Manager Agent. The model itself is whichever `llm`
provider config/providers.yaml has active (Ollama by default,
Anthropic/others a config change away) — this module never imports a
vendor SDK directly, only `libs.providers.get_provider("llm")`.

Unlike the Manager's reasoning engine, there is no deterministic fallback
here. A missing/misconfigured provider, a failed call, a refusal, or a
malformed response all raise `IdeaGenerationError` instead of returning a
placeholder idea — inventing a fake "fallback idea" would defeat the
entire purpose of an agent whose only job is finding real ideas. The
error propagates up through `ResearchAgent.run()` as a normal job
failure, which the Manager's own reasoning engine then decides how to
handle (retry a transient failure, escalate a persistent one) — exactly
the same path any other agent's failure takes.
"""

from dataclasses import dataclass
from typing import Any

from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.providers.base import ProviderConfigError
from libs.providers.llm.base import LLMProviderError, LLMToolCall
from libs.providers.registry import get_provider

logger = get_logger(__name__)

#: This engine only ever calls the Anthropic API, so it always asks for
#: the "claude" prompt variant if one exists — see
#: reasoning.py's `_PROMPT_PROVIDER` for the identical rationale.
_PROMPT_PROVIDER = "claude"


class IdeaGenerationError(RuntimeError):
    """Idea generation could not produce a result. Never caught silently
    inside this module — always propagates so the caller's job fails
    honestly instead of inventing a placeholder idea.
    """


@dataclass(frozen=True)
class GeneratedIdea:
    topic: str
    target_audience: str
    why_people_would_watch: str
    keywords: list[str]
    suggested_angle: str
    competition_level: str
    score: int
    suggested_length_sec: int
    research_notes: str


_PROPOSE_IDEAS_TOOL = LLMToolCall(
    name="propose_video_ideas",
    description="Propose one or more scored, fully-researched YouTube video ideas for a channel.",
    input_schema={
        "type": "object",
        "properties": {
            "ideas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "The video's working title/topic.",
                        },
                        "target_audience": {
                            "type": "string",
                            "description": "Who specifically would watch this — not just the channel's general audience.",
                        },
                        "why_people_would_watch": {
                            "type": "string",
                            "description": "The concrete reason this idea earns a click and a watch.",
                        },
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Terms a viewer might search for to find this video.",
                        },
                        "suggested_angle": {
                            "type": "string",
                            "description": "The specific creative hook/framing, not a restatement of the topic.",
                        },
                        "competition_level": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "How saturated this specific angle already is on YouTube.",
                        },
                        "score": {
                            "type": "integer",
                            "description": "Confidence score, 0-100.",
                        },
                        "suggested_length_sec": {
                            "type": "integer",
                            "description": "Suggested video length in seconds.",
                        },
                        "research_notes": {
                            "type": "string",
                            "description": (
                                "What informed this idea — trend signals used, "
                                "tradeoffs, caveats — for the audit trail."
                            ),
                        },
                    },
                    "required": [
                        "topic",
                        "target_audience",
                        "why_people_would_watch",
                        "keywords",
                        "suggested_angle",
                        "competition_level",
                        "score",
                        "suggested_length_sec",
                        "research_notes",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["ideas"],
        "additionalProperties": False,
    },
)


class IdeaGenerator:
    def __init__(self) -> None:
        self._prompts = get_prompt_loader()

    def generate(
        self,
        *,
        project_id: str | None,
        channel_niche: str,
        channel_persona: str,
        banned_topics: list[str] | None,
        existing_titles: list[str] | None,
        trend_signals: list[str] | None,
        goal: str | None,
        count: int,
    ) -> list[GeneratedIdea]:
        try:
            provider = get_provider("llm")
        except ProviderConfigError as exc:
            raise IdeaGenerationError(f"llm provider unavailable: {exc}") from exc

        try:
            system_template = self._prompts.get(
                "research", "generate_ideas_system", provider=_PROMPT_PROVIDER
            )
            system_prompt = system_template.render()
            user_prompt = self._prompts.get(
                "research", "generate_ideas_user", provider=_PROMPT_PROVIDER
            ).render(
                channel_niche=channel_niche,
                channel_persona=channel_persona,
                banned_topics=banned_topics,
                existing_titles=existing_titles,
                trend_signals=trend_signals,
                goal=goal,
                count=count,
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise IdeaGenerationError(f"prompt template error: {exc}") from exc

        try:
            with track_llm_call(
                project_id=project_id,
                agent_name="research",
                call_site="idea_generator.generate",
                provider=type(provider).__name__,
                model=provider.model,
                prompt_name="generate_ideas",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_prompt, user_prompt=user_prompt, tool=_PROPOSE_IDEAS_TOOL,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            logger.error("idea_generator_call_failed", error=str(exc))
            raise IdeaGenerationError(f"LLM call failed: {exc}") from exc

        ideas_raw: list[dict[str, Any]] = result.tool_input["ideas"]
        if not ideas_raw:
            raise IdeaGenerationError("the model returned an empty ideas list")

        return [GeneratedIdea(**idea) for idea in ideas_raw]
