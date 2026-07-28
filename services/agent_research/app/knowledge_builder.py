"""Deep-research Knowledge Package builder for the Research Agent.

Distinct from idea_generator.py: `IdeaGenerator` picks/scores a topic;
`KnowledgeBuilder` researches a topic that has already been chosen,
producing the structured package (verified facts, timeline, entities,
citations, keywords, related topics, hooks, supporting notes — see
libs/schemas/knowledge.py) the Script Agent will write from directly.

The call here is genuinely different in shape from every other call in
this pipeline (reasoning.py, idea_generator.py), for one reason:
"verified facts" and real citations require the model to actually look
something up, not recall it from parametric memory — so this passes
`enable_web_research=True` to `generate_tool_call()` (libs/providers/llm/
base.py), which lets a provider that has server-side web tools (Anthropic
does; see anthropic_provider.py's `_generate_with_web_research`) use them
across as many turns as the topic needs before finally reporting a
package. All of that multi-turn mechanics (continuations on a
`pause_turn`, tool wiring, summing usage across turns) lives inside the
provider, not here — this module only ever sees one `LLMToolResult` back,
the same as every other call site in this codebase.

A provider with no such capability (e.g. Ollama/qwen3 — no local
equivalent of server-side web search exists in this environment) still
answers, best-effort, from its own parametric knowledge instead of
raising (see `generate_tool_call`'s docstring) — which means a package
built under a non-web-research-capable provider is not actually
externally verified, whatever its `confidence`/`sources` fields claim.
`_to_package` below appends an explicit caveat to `supporting_notes` in
that case so nothing downstream (the Script Agent, a human operator)
mistakes local-model recall for real citations.

No deterministic fallback exists here, for the same reason
IdeaGenerator has none — more so, in fact: fabricating "verified facts"
with fake citations when the real call fails would be actively harmful,
not just low-value. Every failure mode raises `KnowledgePackageError`
and fails the job honestly.
"""

from typing import Any

from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.providers.base import ProviderConfigError
from libs.providers.llm.base import LLMProviderError, LLMToolCall
from libs.providers.registry import get_provider
from libs.schemas.knowledge import Entity, KnowledgePackage, TimelineEntry, VerifiedFact

logger = get_logger(__name__)

_PROMPT_PROVIDER = "claude"

#: Shown to whoever reads a package built without real web research —
#: see this module's own docstring.
_NO_WEB_RESEARCH_CAVEAT = (
    "UNVERIFIED: the configured llm provider has no web research capability, "
    "so this package reflects the model's own parametric knowledge, not "
    "live-verified sources — treat every 'fact'/citation here as unconfirmed."
)

_PROPOSE_KNOWLEDGE_PACKAGE_TOOL = LLMToolCall(
    name="propose_knowledge_package",
    description=(
        "Submit the completed knowledge package for this video topic, "
        "once research is actually done — not before."
    ),
    input_schema={
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
)


class KnowledgePackageError(RuntimeError):
    """Knowledge package generation could not produce a result. Never
    caught silently inside this module — always propagates so the
    caller's job fails honestly instead of shipping fabricated research.
    """


class KnowledgeBuilder:
    def __init__(self) -> None:
        self._prompts = get_prompt_loader()

    def build(
        self,
        *,
        project_id: str,
        topic: str,
        target_audience: str,
        channel_niche: str,
        channel_persona: str,
        banned_topics: list[str] | None,
        existing_keywords: list[str] | None,
    ) -> KnowledgePackage:
        try:
            provider = get_provider("llm")
        except ProviderConfigError as exc:
            raise KnowledgePackageError(f"llm provider unavailable: {exc}") from exc

        try:
            system_template = self._prompts.get(
                "research", "build_knowledge_package_system", provider=_PROMPT_PROVIDER
            )
            system_prompt = system_template.render()
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

        try:
            with track_llm_call(
                project_id=project_id,
                agent_name="research",
                call_site="knowledge_builder.build",
                provider=type(provider).__name__,
                model=provider.model,
                prompt_name="build_knowledge_package",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tool=_PROPOSE_KNOWLEDGE_PACKAGE_TOOL,
                    enable_web_research=True,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            logger.error("knowledge_builder_call_failed", error=str(exc))
            raise KnowledgePackageError(f"LLM call failed: {exc}") from exc

        return self._to_package(
            result.tool_input,
            topic=topic,
            niche=channel_niche,
            used_web_research=result.used_web_research,
        )

    @staticmethod
    def _to_package(
        raw: dict[str, Any], *, topic: str, niche: str, used_web_research: bool
    ) -> KnowledgePackage:
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

        supporting_notes = raw["supporting_notes"]
        if not used_web_research:
            supporting_notes = f"{_NO_WEB_RESEARCH_CAVEAT}\n\n{supporting_notes}"

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
            supporting_notes=supporting_notes,
        )
