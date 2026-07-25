"""Deep-research Knowledge Package builder for the Research Agent.

Distinct from idea_generator.py: `IdeaGenerator` picks/scores a topic;
`KnowledgeBuilder` researches a topic that has already been chosen,
producing the structured package (verified facts, timeline, entities,
citations, keywords, related topics, hooks, supporting notes — see
libs/schemas/knowledge.py) the Script Agent will write from directly.

The Claude call here is genuinely different in shape from every other
call in this pipeline (reasoning.py, idea_generator.py), for one reason:
"verified facts" and real citations require Claude to actually look
something up, not recall it from parametric memory. So this call
includes Anthropic's server-side `web_search`/`web_fetch` tools alongside
the structured-output tool, and — critically — does **not** force
`tool_choice` the way every other call in this codebase does. Forcing
`tool_choice` to `propose_knowledge_package` would require Claude's very
first action to be that tool call, leaving no room to search the web
first. Leaving `tool_choice` at its default ("auto") lets Claude call
web_search/web_fetch as many times as the topic needs — each one
executed server-side by Anthropic within the same turn, no client-side
work required — before finally calling `propose_knowledge_package` once
it actually has something to report.

That freedom has a real cost: a long research turn can pause mid-way
with `stop_reason: "pause_turn"` rather than finishing (see
`shared/tool-use-concepts.md`'s server-tools guidance in the claude-api
skill). This module handles that by resending the paused conversation
with a bounded number of continuations, per Anthropic's documented
pattern, rather than treating a pause as a failure.

No deterministic fallback exists here, for the same reason
IdeaGenerator has none — more so, in fact: fabricating "verified facts"
with fake citations when the real call fails would be actively harmful,
not just low-value. Every failure mode raises `KnowledgePackageError`
and fails the job honestly.
"""

from typing import Any

import anthropic

from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.schemas.knowledge import Entity, KnowledgePackage, TimelineEntry, VerifiedFact

logger = get_logger(__name__)

_PROMPT_PROVIDER = "claude"

#: Server-side tools — Anthropic executes these itself within the same
#: turn; there is no client-side function to implement for either. Exact
#: tool-type strings and shape per the claude-api skill's
#: shared/tool-use-concepts.md (dynamic-filtering versions, Opus 5).
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

#: How many times to resend a `pause_turn`-interrupted research turn
#: before giving up. Bounds worst-case latency/cost on a topic that keeps
#: triggering long server-tool turns.
_MAX_CONTINUATIONS = 3

_PROPOSE_KNOWLEDGE_PACKAGE_TOOL = {
    "name": "propose_knowledge_package",
    "description": (
        "Submit the completed knowledge package for this video topic, "
        "once research is actually done — not before."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A short paragraph tying the topic together for someone who hasn't researched it.",
            },
            "verified_facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "URLs a web_search/web_fetch result actually returned this turn. Empty if none was found — never invent one.",
                        },
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["statement", "sources", "confidence"],
                    "additionalProperties": False,
                },
            },
            "timeline": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "As precise as the source allows — a year, a quarter, a full date, or \"undated\".",
                        },
                        "event": {"type": "string"},
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["date", "event", "sources"],
                    "additionalProperties": False,
                },
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "description": "e.g. person, organization, product, place, concept — free text, not a fixed list.",
                        },
                        "description": {"type": "string"},
                    },
                    "required": ["name", "type", "description"],
                    "additionalProperties": False,
                },
            },
            "keywords": {"type": "array", "items": {"type": "string"}},
            "related_topics": {"type": "array", "items": {"type": "string"}},
            "hooks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific, usable opening/angle ideas — not generic advice like \"make it engaging.\"",
            },
            "supporting_notes": {
                "type": "string",
                "description": "Caveats, source disagreements, research gaps — anything the Script Agent should know.",
            },
        },
        "required": [
            "summary",
            "verified_facts",
            "timeline",
            "entities",
            "keywords",
            "related_topics",
            "hooks",
            "supporting_notes",
        ],
        "additionalProperties": False,
    },
}


class KnowledgePackageError(RuntimeError):
    """Knowledge package generation could not produce a result. Never
    caught silently inside this module — always propagates so the
    caller's job fails honestly instead of shipping fabricated research.
    """


class KnowledgeBuilder:
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

    def build(
        self,
        *,
        topic: str,
        target_audience: str,
        channel_niche: str,
        channel_persona: str,
        banned_topics: list[str] | None,
        existing_keywords: list[str] | None,
    ) -> KnowledgePackage:
        if self._client is None:
            raise KnowledgePackageError("ANTHROPIC_API_KEY is not configured")

        try:
            system_prompt = self._prompts.get(
                "research", "build_knowledge_package_system", provider=_PROMPT_PROVIDER
            ).render()
            user_prompt = self._prompts.get(
                "research", "build_knowledge_package_user", provider=_PROMPT_PROVIDER
            ).render(
                topic=topic,
                target_audience=target_audience,
                channel_niche=channel_niche,
                channel_persona=channel_persona,
                banned_topics=banned_topics,
                existing_keywords=existing_keywords,
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise KnowledgePackageError(f"prompt template error: {exc}") from exc

        raw = self._research_until_reported(system_prompt, user_prompt)
        return self._to_package(raw, topic=topic, niche=channel_niche)

    def _research_until_reported(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

        for attempt in range(_MAX_CONTINUATIONS + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=8192,
                    output_config={"effort": self._effort},
                    system=system_prompt,
                    tools=[_WEB_SEARCH_TOOL, _WEB_FETCH_TOOL, _PROPOSE_KNOWLEDGE_PACKAGE_TOOL],
                    messages=messages,
                )
            except anthropic.APIError as exc:
                logger.error("knowledge_builder_call_failed", error=str(exc), attempt=attempt)
                raise KnowledgePackageError(f"Claude API call failed: {exc}") from exc

            if response.stop_reason == "refusal":
                logger.warning("knowledge_builder_refusal")
                raise KnowledgePackageError("Claude declined to respond")

            tool_use = next(
                (
                    block
                    for block in response.content
                    if block.type == "tool_use" and block.name == "propose_knowledge_package"
                ),
                None,
            )
            if tool_use is not None:
                return tool_use.input

            if response.stop_reason == "pause_turn":
                logger.info("knowledge_builder_pause_turn_continuing", attempt=attempt)
                messages.append({"role": "assistant", "content": response.content})
                continue

            logger.error(
                "knowledge_builder_no_tool_use", stop_reason=response.stop_reason, attempt=attempt
            )
            raise KnowledgePackageError(
                f"Claude did not return a knowledge package (stop_reason={response.stop_reason})"
            )

        raise KnowledgePackageError(
            f"Claude did not finish researching within {_MAX_CONTINUATIONS} continuations"
        )

    @staticmethod
    def _to_package(raw: dict[str, Any], *, topic: str, niche: str) -> KnowledgePackage:
        facts = [VerifiedFact(**f) for f in raw["verified_facts"]]
        timeline = [TimelineEntry(**t) for t in raw["timeline"]]
        entities = [Entity(**e) for e in raw["entities"]]

        citations: list[str] = []
        seen: set[str] = set()
        for source_list in [f.sources for f in facts] + [t.sources for t in timeline]:
            for url in source_list:
                if url not in seen:
                    seen.add(url)
                    citations.append(url)

        return KnowledgePackage(
            topic=topic,
            niche=niche,
            summary=raw["summary"],
            verified_facts=facts,
            timeline=timeline,
            entities=entities,
            citations=citations,
            keywords=raw["keywords"],
            related_topics=raw["related_topics"],
            hooks=raw["hooks"],
            supporting_notes=raw["supporting_notes"],
        )
