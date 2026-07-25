"""Claude-backed idea generation for the Research Agent.

Both prompts sent to Claude are loaded at runtime from the Prompt
Management System (libs/prompts) rather than embedded as Python string
constants — see prompts/research/generate_ideas_system/ and
prompts/research/generate_ideas_user/, and
services/orchestrator/app/manager/reasoning.py for the same pattern
applied to the Manager Agent.

Unlike the Manager's reasoning engine, there is no deterministic fallback
here. A missing API key, a failed call, a refusal, or a malformed response
all raise `IdeaGenerationError` instead of returning a placeholder idea —
inventing a fake "fallback idea" would defeat the entire purpose of an
agent whose only job is finding real ideas. The error propagates up
through `ResearchAgent.run()` as a normal job failure, which the Manager's
own reasoning engine then decides how to handle (retry a transient
failure, escalate a persistent one) — exactly the same path any other
agent's failure takes.
"""

from dataclasses import dataclass
from typing import Any

import anthropic

from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader

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


_PROPOSE_IDEAS_TOOL = {
    "name": "propose_video_ideas",
    "description": "Propose one or more scored, fully-researched YouTube video ideas for a channel.",
    "strict": True,
    "input_schema": {
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
}


class IdeaGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._prompts = get_prompt_loader()
        self._client = (
            anthropic.Anthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )

    def generate(
        self,
        *,
        channel_niche: str,
        channel_persona: str,
        banned_topics: list[str] | None,
        existing_titles: list[str] | None,
        trend_signals: list[str] | None,
        goal: str | None,
        count: int,
    ) -> list[GeneratedIdea]:
        if self._client is None:
            raise IdeaGenerationError("ANTHROPIC_API_KEY is not configured")

        try:
            system_prompt = self._prompts.get(
                "research", "generate_ideas_system", provider=_PROMPT_PROVIDER
            ).render()
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
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                output_config={"effort": self._effort},
                system=system_prompt,
                tools=[_PROPOSE_IDEAS_TOOL],
                tool_choice={"type": "tool", "name": "propose_video_ideas"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIError as exc:
            logger.error("idea_generator_call_failed", error=str(exc))
            raise IdeaGenerationError(f"Claude API call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            logger.warning("idea_generator_refusal")
            raise IdeaGenerationError("Claude declined to respond")

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            logger.error("idea_generator_no_tool_use", stop_reason=response.stop_reason)
            raise IdeaGenerationError(
                f"Claude did not return any ideas (stop_reason={response.stop_reason})"
            )

        ideas_raw: list[dict[str, Any]] = tool_use.input["ideas"]
        if not ideas_raw:
            raise IdeaGenerationError("Claude returned an empty ideas list")

        return [GeneratedIdea(**idea) for idea in ideas_raw]
