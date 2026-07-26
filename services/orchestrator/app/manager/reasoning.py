"""Claude-backed reasoning engine for workflow decisions.

The Manager's decision at every stage transition — advance, retry,
escalate, or abort — is made by calling Claude with the outcome of the
project's most recently finished stage. The fixed plan (workflow.py) is
ground truth for *what "advance" means*; Claude's job is judgment, not
routing — that matters most on failure, where "is this worth retrying" is
a real call a hardcoded rule can't make well: a rate-limit error and a
permanent configuration error can look identical to a naive string match,
but call for opposite responses.

Both prompts sent to Claude — the system prompt and the per-decision
context — are loaded at runtime from the Prompt Management System
(libs/prompts) rather than embedded as Python string constants here; see
prompts/manager/workflow_decision_system/ and
prompts/manager/workflow_decision_user/. This is what lets the wording be
tuned, versioned (MANAGER_PROMPT_VERSION), or given Claude-specific
phrasing (v1.claude.yaml) without a code change. The tool's JSON schema
below stays in Python: it's a structural API contract (types, required
fields, strict validation), not natural-language template content.

If the Claude API call itself fails — network error, exhausted retries,
a safety refusal — this falls back to a small deterministic rule instead
of raising, so a reasoning-engine outage can never wedge the pipeline.
That fallback is also exactly what runs when no API key is configured at
all, which is what this module's tests exercise without a live key.
"""

from dataclasses import dataclass

import anthropic

from libs.core.config import get_settings
from libs.core.logging import get_logger
from libs.llm_usage import track_llm_call
from libs.models.enums import JobStatus
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader

logger = get_logger(__name__)


class WorkflowAction:
    ADVANCE = "advance"
    RETRY = "retry"
    ESCALATE = "escalate"
    ABORT = "abort"


@dataclass(frozen=True)
class Decision:
    action: str
    reasoning: str


_DECIDE_ACTION_TOOL = {
    "name": "decide_workflow_action",
    "description": (
        "Decide what the pipeline manager should do next for a project, "
        "given the outcome of its most recently completed stage."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    WorkflowAction.ADVANCE,
                    WorkflowAction.RETRY,
                    WorkflowAction.ESCALATE,
                    WorkflowAction.ABORT,
                ],
                "description": (
                    "advance: proceed to the next stage in the fixed plan. "
                    "retry: re-run the current stage from scratch. "
                    "escalate: stop automation and flag the project for "
                    "human review. abort: stop automation and mark the "
                    "project failed."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One or two sentences explaining the decision, for the "
                    "audit log (system_events)."
                ),
            },
        },
        "required": ["action", "reasoning"],
        "additionalProperties": False,
    },
}


#: The provider identifier this engine requests from the Prompt
#: Management System. This engine only ever calls the Anthropic API, so
#: it always asks for the "claude" prompt variant if one exists (falling
#: back to the default otherwise — see libs/prompts/loader.py). If a
#: second reasoning-engine backend is ever added, it should pass its own
#: provider name here instead.
_PROMPT_PROVIDER = "claude"


class ReasoningEngine:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._prompt_version = settings.manager_prompt_version
        self._prompts = get_prompt_loader()
        self._client = (
            anthropic.Anthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )

    def decide(
        self,
        *,
        project_id: str,
        phase: str,
        stage: str,
        job_status: str,
        error: str | None,
        retry_count: int,
        max_retries: int,
        next_stage: str | None,
    ) -> Decision:
        if self._client is None:
            return self._fallback(
                job_status, retry_count, max_retries, reason="ANTHROPIC_API_KEY is not configured"
            )

        try:
            system_template = self._prompts.get(
                "manager",
                "workflow_decision_system",
                version=self._prompt_version,
                provider=_PROMPT_PROVIDER,
            )
            system_prompt = system_template.render()
            prompt = self._prompts.get(
                "manager",
                "workflow_decision_user",
                version=self._prompt_version,
                provider=_PROMPT_PROVIDER,
            ).render(
                phase=phase,
                stage=stage,
                job_status=job_status,
                error=error,
                retry_count=retry_count,
                max_retries=max_retries,
                next_stage=next_stage,
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            # A missing/broken template file is exactly the kind of
            # reasoning-engine failure this fallback exists for — it must
            # not be able to wedge the pipeline any more than a Claude API
            # outage would.
            logger.error("reasoning_engine_prompt_load_failed", error=str(exc))
            return self._fallback(
                job_status, retry_count, max_retries, reason=f"Prompt template error: {exc}"
            )

        try:
            with track_llm_call(
                project_id=project_id,
                agent_name="manager",
                call_site="reasoning.decide",
                model=self._model,
                prompt_name="workflow_decision",
                prompt_version=system_template.version,
            ) as usage:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    output_config={"effort": self._effort},
                    system=system_prompt,
                    tools=[_DECIDE_ACTION_TOOL],
                    tool_choice={"type": "tool", "name": "decide_workflow_action"},
                    messages=[{"role": "user", "content": prompt}],
                )
                usage["input_tokens"] = response.usage.input_tokens
                usage["output_tokens"] = response.usage.output_tokens
        except anthropic.APIError as exc:
            logger.error("reasoning_engine_call_failed", error=str(exc))
            return self._fallback(
                job_status, retry_count, max_retries, reason=f"Claude API call failed: {exc}"
            )

        if response.stop_reason == "refusal":
            logger.warning("reasoning_engine_refusal")
            return self._fallback(
                job_status, retry_count, max_retries, reason="Claude declined to respond"
            )

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            logger.error("reasoning_engine_no_tool_use", stop_reason=response.stop_reason)
            return self._fallback(
                job_status,
                retry_count,
                max_retries,
                reason=f"Claude did not return a decision (stop_reason={response.stop_reason})",
            )

        return Decision(action=tool_use.input["action"], reasoning=tool_use.input["reasoning"])

    @staticmethod
    def _fallback(job_status: str, retry_count: int, max_retries: int, *, reason: str) -> Decision:
        """The deterministic safety net: advance on success, retry while
        the project's retry budget allows it, escalate once it doesn't.
        """
        if job_status == JobStatus.SUCCEEDED.value:
            action = WorkflowAction.ADVANCE
        elif retry_count < max_retries:
            action = WorkflowAction.RETRY
        else:
            action = WorkflowAction.ESCALATE
        return Decision(action=action, reasoning=f"Deterministic fallback used ({reason}).")
