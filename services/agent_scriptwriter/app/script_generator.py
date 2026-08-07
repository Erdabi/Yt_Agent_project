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

One thing this module does do beyond a single call: when
`script_from_dict()` rejects the draft, it hands the rejected script and
the exact rejection message back to the model and asks for that one
defect to be fixed (`SCRIPT_REPAIR_ATTEMPTS`, default 2). Those
validations are deterministic and their messages name the offending beat
precisely — so the information needed to fix the script already exists at
the moment of failure, and throwing it away to resample the identical
prompt wastes the best signal available. Repair is bounded, never
weakens or skips a validation (the repaired script goes through exactly
the same `script_from_dict()`), and a script that still fails after the
last attempt raises `ScriptGenerationError` exactly as it did before.
"""

import json

from libs.context import ProjectContext
from libs.core.config import get_settings
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

        return self._parse_with_repair(result.tool_input, context)

    def _parse_with_repair(self, payload: dict, context: ProjectContext) -> GeneratedScript:
        """Validates `payload`, and on rejection asks the model to fix
        the specific defect the validator named, up to
        `SCRIPT_REPAIR_ATTEMPTS` times.

        The validation itself is untouched on every pass — a repaired
        script has to satisfy exactly the same `script_from_dict()` as
        the original, so this can only ever turn a rejected script into a
        genuinely valid one, never into an accepted-but-invalid one.
        """
        attempts_left = max(get_settings().script_repair_attempts, 0)
        while True:
            try:
                return script_from_dict(payload)
            except ValueError as exc:
                if attempts_left <= 0:
                    logger.error("script_generator_invalid_script", error=str(exc))
                    raise ScriptGenerationError(str(exc)) from exc
                logger.warning(
                    "script_generator_repairing",
                    error=str(exc),
                    attempts_left=attempts_left,
                )
                try:
                    payload = self._repair(payload, str(exc), context)
                except ScriptGenerationError as repair_exc:
                    # The repair call itself failed (provider down, bad
                    # template, refusal). Report the original rejection —
                    # that is the script's actual problem — while naming
                    # the repair failure so a genuine outage isn't hidden
                    # behind a validation message.
                    raise ScriptGenerationError(
                        f"{exc} (repair attempt also failed: {repair_exc})"
                    ) from exc
                attempts_left -= 1

    def _repair(self, rejected: dict, validation_error: str, context: ProjectContext) -> dict:
        provider = get_provider("llm")
        try:
            system_template = self._prompts.get(
                "script",
                "repair_script_system",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            )
            user_prompt = self._prompts.get(
                "script",
                "repair_script_user",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            ).render(
                validation_error=validation_error,
                rejected_script=json.dumps(rejected, indent=2, ensure_ascii=False),
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise ScriptGenerationError(f"prompt template error: {exc}") from exc

        try:
            with track_llm_call(
                project_id=context.project.project_id,
                agent_name="script",
                call_site="script_generator.repair",
                provider=type(provider).__name__,
                model=provider.model,
                prompt_name="repair_script",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_template.render(),
                    user_prompt=user_prompt,
                    tool=_PROPOSE_SCRIPT_TOOL,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            raise ScriptGenerationError(f"LLM call failed: {exc}") from exc

        if not result.tool_input.get("main_sections"):
            raise ScriptGenerationError("the repair returned a script with no main sections")
        return result.tool_input
