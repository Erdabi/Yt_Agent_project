"""Claude-backed script generation for the Script Agent.

Turns one researched, approved video idea — delivered as a single
`ProjectContext` (libs/context) rather than queried piecemeal — into a
complete, production-ready script: a strong opening hook, an
introduction, a deliberately chosen story structure, the main body
sections, retention techniques woven throughout, an ending, and a call
to action. Every beat carries voice-over text, a scene description, and
concrete visual suggestions, for the Video Agent's storyboard/voiceover
modules (services/agent_video) to work from directly once implemented.

Both prompts sent to Claude are loaded at runtime from the Prompt
Management System (libs/prompts) — see
prompts/script/generate_script_system/ and
prompts/script/generate_script_user/ — following the same forced
tool-use, no-fallback pattern as
services/agent_research/app/idea_generator.py: a missing API key, a
failed call, a refusal, or a malformed response all raise
`ScriptGenerationError` instead of returning a placeholder script —
inventing a fake script would defeat the entire purpose of this agent.
"""

from dataclasses import dataclass

import anthropic

from libs.context import ProjectContext
from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.schemas.knowledge import KnowledgePackage

logger = get_logger(__name__)

#: This engine only ever calls the Anthropic API, so it always asks for
#: the "claude" prompt variant if one exists — see
#: reasoning.py's `_PROMPT_PROVIDER` for the identical rationale.
_PROMPT_PROVIDER = "claude"


class ScriptGenerationError(RuntimeError):
    """Script generation could not produce a result. Never caught
    silently inside this module — always propagates so the caller's job
    fails honestly instead of shipping a fabricated/placeholder script.
    """


@dataclass(frozen=True)
class ScriptBeat:
    voiceover_text: str
    scene_description: str
    visual_suggestions: str


@dataclass(frozen=True)
class ScriptSection(ScriptBeat):
    #: A short internal label (e.g. "Why this happens") — never shown to
    #: viewers, just for readability in the DB/audit trail.
    heading: str


@dataclass(frozen=True)
class GeneratedScript:
    structure_notes: str
    retention_notes: str
    hook: ScriptBeat
    introduction: ScriptBeat
    main_sections: list[ScriptSection]
    ending: ScriptBeat
    call_to_action: ScriptBeat


_BEAT_PROPERTIES = {
    "voiceover_text": {
        "type": "string",
        "description": (
            "Exact narration for this beat, written for spoken delivery — "
            "short sentences, natural rhythm, not prose meant to be read."
        ),
    },
    "scene_description": {
        "type": "string",
        "description": "What is happening on screen during this beat, for whoever storyboards it next.",
    },
    "visual_suggestions": {
        "type": "string",
        "description": (
            "Concrete visual ideas for this beat (b-roll, on-screen text, a "
            'specific shot) — never a generic "add engaging visuals."'
        ),
    },
}
_BEAT_REQUIRED = ["voiceover_text", "scene_description", "visual_suggestions"]

_BEAT_SCHEMA = {
    "type": "object",
    "properties": _BEAT_PROPERTIES,
    "required": _BEAT_REQUIRED,
    "additionalProperties": False,
}

_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {
            "type": "string",
            "description": "A short internal label for this section (not shown to viewers).",
        },
        **_BEAT_PROPERTIES,
    },
    "required": ["heading", *_BEAT_REQUIRED],
    "additionalProperties": False,
}

_PROPOSE_SCRIPT_TOOL = {
    "name": "propose_script",
    "description": "Propose a complete, structured YouTube video script ready for production.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "structure_notes": {
                "type": "string",
                "description": (
                    "The narrative structure/story arc chosen for this script "
                    '(e.g. "problem -> agitation -> solution", "chronological '
                    'case study", "listicle with a throughline") and why it '
                    "fits this topic."
                ),
            },
            "retention_notes": {
                "type": "string",
                "description": (
                    "The specific retention techniques used and where in the "
                    "script (open loops, pattern interrupts, callbacks, "
                    "curiosity gaps) — concrete, tied to moments in the "
                    "script, not generic advice."
                ),
            },
            "hook": _BEAT_SCHEMA,
            "introduction": _BEAT_SCHEMA,
            "main_sections": {
                "type": "array",
                "items": _SECTION_SCHEMA,
                "minItems": 1,
            },
            "ending": _BEAT_SCHEMA,
            "call_to_action": _BEAT_SCHEMA,
        },
        "required": [
            "structure_notes",
            "retention_notes",
            "hook",
            "introduction",
            "main_sections",
            "ending",
            "call_to_action",
        ],
        "additionalProperties": False,
    },
}


def _render_knowledge_package_context(package: KnowledgePackage) -> str:
    """A compact plain-text digest of a `KnowledgePackage` for the prompt
    — not the full Markdown rendering
    (services/agent_research/app/knowledge_package_render.py), which is
    Research-Agent-specific output for humans; this agent only needs the
    content-generation inputs (facts, timeline, entities, hooks), not
    citations or schema metadata.
    """
    lines: list[str] = [f"Summary: {package.summary}"]

    if package.verified_facts:
        lines.append("Verified facts:")
        lines.extend(
            f"- {fact.statement} ({fact.confidence} confidence)" for fact in package.verified_facts
        )
    if package.timeline:
        lines.append("Timeline:")
        lines.extend(f"- {entry.date}: {entry.event}" for entry in package.timeline)
    if package.entities:
        lines.append("Key entities:")
        lines.extend(f"- {e.name} ({e.type}): {e.description}" for e in package.entities)
    if package.hooks:
        lines.append("Suggested hooks from research:")
        lines.extend(f"- {hook}" for hook in package.hooks)
    if package.related_topics:
        lines.append(f"Related topics: {', '.join(package.related_topics)}")
    if package.supporting_notes:
        lines.append(f"Caveats/notes: {package.supporting_notes}")

    return "\n".join(lines)


def _beat_from(data: dict) -> ScriptBeat:
    return ScriptBeat(
        voiceover_text=data["voiceover_text"],
        scene_description=data["scene_description"],
        visual_suggestions=data["visual_suggestions"],
    )


def _to_generated_script(raw: dict) -> GeneratedScript:
    return GeneratedScript(
        structure_notes=raw["structure_notes"],
        retention_notes=raw["retention_notes"],
        hook=_beat_from(raw["hook"]),
        introduction=_beat_from(raw["introduction"]),
        main_sections=[
            ScriptSection(
                voiceover_text=section["voiceover_text"],
                scene_description=section["scene_description"],
                visual_suggestions=section["visual_suggestions"],
                heading=section["heading"],
            )
            for section in raw["main_sections"]
        ],
        ending=_beat_from(raw["ending"]),
        call_to_action=_beat_from(raw["call_to_action"]),
    )


class ScriptGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        self._prompts = get_prompt_loader()
        self._client = (
            anthropic.Anthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )

    def generate(self, context: ProjectContext) -> GeneratedScript:
        if self._client is None:
            raise ScriptGenerationError("ANTHROPIC_API_KEY is not configured")

        try:
            system_template = self._prompts.get(
                "script",
                "generate_script_system",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            )
            system_prompt = system_template.render()
            user_prompt = self._prompts.get(
                "script",
                "generate_script_user",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            ).render(
                topic=context.research.topic,
                description=context.research.description,
                target_audience=context.research.target_audience,
                channel_niche=context.channel.niche,
                channel_persona=context.channel.persona,
                banned_topics=context.channel.banned_topics,
                suggested_angle=context.research.suggested_angle,
                keywords=context.research.keywords,
                research_notes=context.research.research_notes,
                target_duration_sec=context.research.suggested_length_sec,
                knowledge_package_context=(
                    _render_knowledge_package_context(context.knowledge_package)
                    if context.knowledge_package
                    else None
                ),
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise ScriptGenerationError(f"prompt template error: {exc}") from exc

        try:
            with track_llm_call(
                project_id=context.project.project_id,
                agent_name="script",
                call_site="script_generator.generate",
                model=context.manager.anthropic_model,
                prompt_name="generate_script",
                prompt_version=system_template.version,
            ) as usage:
                response = self._client.messages.create(
                    model=context.manager.anthropic_model,
                    max_tokens=8192,
                    output_config={"effort": context.manager.anthropic_effort},
                    system=system_prompt,
                    tools=[_PROPOSE_SCRIPT_TOOL],
                    tool_choice={"type": "tool", "name": "propose_script"},
                    messages=[{"role": "user", "content": user_prompt}],
                )
                usage["input_tokens"] = response.usage.input_tokens
                usage["output_tokens"] = response.usage.output_tokens
        except anthropic.APIError as exc:
            logger.error("script_generator_call_failed", error=str(exc))
            raise ScriptGenerationError(f"Claude API call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            logger.warning("script_generator_refusal")
            raise ScriptGenerationError("Claude declined to respond")

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            logger.error("script_generator_no_tool_use", stop_reason=response.stop_reason)
            raise ScriptGenerationError(
                f"Claude did not return a script (stop_reason={response.stop_reason})"
            )

        if not tool_use.input["main_sections"]:
            raise ScriptGenerationError("Claude returned a script with no main sections")

        return _to_generated_script(tool_use.input)
