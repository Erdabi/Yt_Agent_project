"""LLM-backed script generation for the Script Agent.

Turns one researched, approved video idea — delivered as a single
`ProjectContext` (libs/context) rather than queried piecemeal — into a
complete, production-ready script: a strong opening hook, an
introduction, a deliberately chosen story structure, the main body
sections, retention techniques woven throughout, an ending, and a call
to action. Every beat carries voice-over text, a scene description, and
structured production metadata (camera framing, a provider-independent
list of asset requirements, transition, pacing, narration emotion,
emphasis words, speech speed — see script_schema.py) for the Video
Agent's storyboard/voiceover modules (services/agent_video) to consume
directly, without parsing free text.

This module only drafts. `ScriptwriterAgent.run()` (worker.py) always
runs `script_reviewer.py`'s self-review pass on this draft afterward
before persisting — see that module for why review is a separate,
best-effort step rather than folded into this call.

Both prompts sent to the model are loaded at runtime from the Prompt
Management System (libs/prompts) — see
prompts/script/generate_script_system/ and
prompts/script/generate_script_user/ — following the same forced
tool-use, no-fallback pattern as
services/agent_research/app/idea_generator.py: a missing/misconfigured
provider, a failed call, a refusal, or a malformed response all raise
`ScriptGenerationError` instead of returning a placeholder script —
inventing a fake script would defeat the entire purpose of this agent.
The model itself is whichever `llm` provider config/providers.yaml has
active (Ollama by default) — this module never imports a vendor SDK
directly, only `libs.providers.get_provider("llm")`.
"""

from libs.context import ProjectContext
from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.providers.base import ProviderConfigError
from libs.providers.llm.base import LLMProviderError, LLMToolCall
from libs.providers.registry import get_provider

from .script_schema import (
    SCRIPT_CONTENT_PROPERTIES,
    SCRIPT_CONTENT_REQUIRED,
    GeneratedScript,
    render_knowledge_package_context,
    script_from_dict,
)

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


_PROPOSE_SCRIPT_TOOL = LLMToolCall(
    name="propose_script",
    description="Propose a complete, structured YouTube video script ready for production.",
    input_schema={
        "type": "object",
        "properties": SCRIPT_CONTENT_PROPERTIES,
        "required": SCRIPT_CONTENT_REQUIRED,
        "additionalProperties": False,
    },
)


class ScriptGenerator:
    def __init__(self) -> None:
        self._prompts = get_prompt_loader()

    def generate(self, context: ProjectContext) -> GeneratedScript:
        try:
            provider = get_provider("llm")
        except ProviderConfigError as exc:
            raise ScriptGenerationError(f"llm provider unavailable: {exc}") from exc

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
                    render_knowledge_package_context(context.knowledge_package)
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
                provider=type(provider).__name__,
                model=provider.model,
                prompt_name="generate_script",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_prompt, user_prompt=user_prompt, tool=_PROPOSE_SCRIPT_TOOL,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            logger.error("script_generator_call_failed", error=str(exc))
            raise ScriptGenerationError(f"LLM call failed: {exc}") from exc

        if not result.tool_input["main_sections"]:
            raise ScriptGenerationError("the model returned a script with no main sections")

        try:
            return script_from_dict(result.tool_input)
        except ValueError as exc:
            logger.error("script_generator_invalid_script", error=str(exc))
            raise ScriptGenerationError(str(exc)) from exc
