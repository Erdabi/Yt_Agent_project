"""Self-review pass for the Script Agent's drafted script.

Runs after script_generator.py produces a draft, before
`ScriptwriterAgent.run()` (worker.py) persists anything: checks the
draft for factual consistency against the Knowledge Package, viewer
retention (a hook that actually earns attention, open loops that get
resolved, retention techniques that are actually present in the
segments rather than just claimed in `retention_notes`), and repetition
across segments — then returns an improved script, corrected where
needed and left alone where it already worked.

Unlike script_generator.py, a failure here does not fail the job: the
draft it's reviewing is already a valid, complete script, and review is
an enhancement on top of it, not the core deliverable. If the review
call fails for any reason (no llm provider configured, a prompt error,
an API error, a refusal, or a malformed response), this logs a warning
and returns the original draft unchanged — with `review_notes` recording
that review was skipped and why, rather than raising. A "lightweight"
review step must not become a hard dependency that can block an
already-good script from being persisted.
"""

import dataclasses
import json

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
    script_to_dict,
)

logger = get_logger(__name__)

#: This engine only ever calls the Anthropic API, so it always asks for
#: the "claude" prompt variant if one exists — see
#: reasoning.py's `_PROMPT_PROVIDER` for the identical rationale.
_PROMPT_PROVIDER = "claude"

_SUBMIT_REVIEWED_SCRIPT_TOOL = LLMToolCall(
    name="submit_reviewed_script",
    description="Submit the reviewed (and, where needed, corrected) script, complete.",
    input_schema={
        "type": "object",
        "properties": {
            **SCRIPT_CONTENT_PROPERTIES,
            "review_notes": {
                "type": "string",
                "description": (
                    "What you checked and what, if anything, you changed. "
                    "Say so plainly if nothing needed fixing rather than "
                    "inventing changes."
                ),
            },
        },
        "required": [*SCRIPT_CONTENT_REQUIRED, "review_notes"],
        "additionalProperties": False,
    },
)


class ScriptReviewer:
    def __init__(self) -> None:
        self._prompts = get_prompt_loader()

    def review(self, context: ProjectContext, draft: GeneratedScript) -> GeneratedScript:
        try:
            provider = get_provider("llm")
        except ProviderConfigError as exc:
            return self._skipped(draft, f"llm provider unavailable: {exc}")

        try:
            system_template = self._prompts.get(
                "script",
                "review_script_system",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            )
            system_prompt = system_template.render()
            user_prompt = self._prompts.get(
                "script",
                "review_script_user",
                version=context.prompt_version,
                provider=_PROMPT_PROVIDER,
            ).render(
                draft_script_json=json.dumps(script_to_dict(draft), indent=2),
                knowledge_package_context=(
                    render_knowledge_package_context(context.knowledge_package)
                    if context.knowledge_package
                    else None
                ),
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            logger.warning("script_reviewer_prompt_load_failed", error=str(exc))
            return self._skipped(draft, f"prompt template error: {exc}")

        try:
            with track_llm_call(
                project_id=context.project.project_id,
                agent_name="script",
                call_site="script_reviewer.review",
                provider=type(provider).__name__,
                model=provider.model,
                prompt_name="review_script",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tool=_SUBMIT_REVIEWED_SCRIPT_TOOL,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            logger.warning("script_reviewer_call_failed", error=str(exc))
            return self._skipped(draft, f"LLM call failed: {exc}")

        if not result.tool_input["main_sections"]:
            logger.warning("script_reviewer_empty_main_sections")
            return self._skipped(draft, "review response had no main sections")

        try:
            return script_from_dict(result.tool_input, review_notes=result.tool_input["review_notes"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("script_reviewer_malformed_response", error=str(exc))
            return self._skipped(draft, f"malformed review response: {exc}")

    @staticmethod
    def _skipped(draft: GeneratedScript, reason: str) -> GeneratedScript:
        """The original draft, unmodified, with `review_notes` recording
        that review didn't run — an operator reading `Script.review_notes`
        later should never have to guess whether a blank value means
        "reviewed, nothing to say" or "never reviewed at all."
        """
        return dataclasses.replace(draft, review_notes=f"Self-review skipped: {reason}")
