"""Claude-backed reasoning engine for workflow decisions.

The Manager's decision at every stage transition — advance, retry,
escalate, or abort — is made by calling Claude with the outcome of the
project's most recently finished stage. The fixed plan (workflow.py) is
ground truth for *what "advance" means*; Claude's job is judgment, not
routing — that matters most on failure, where "is this worth retrying" is
a real call a hardcoded rule can't make well: a rate-limit error and a
permanent configuration error can look identical to a naive string match,
but call for opposite responses.

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
from libs.models.enums import JobStatus

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


_SYSTEM_PROMPT = """\
You are the reasoning engine for an autonomous YouTube video production \
pipeline. A Manager coordinates a fixed sequence of five stages — \
research, script, video (a single agent that internally handles \
storyboard planning, voice-over, assembly, and thumbnail generation as \
one unit of work), quality check, and publishing — each an independent \
worker that reports back success or failure for one project at a time. \
Analytics is not part of this sequence: it runs as its own recurring \
service against already-published projects and is never something you \
are asked to decide about here.

Given the outcome of the stage that just finished for one project, decide \
what the Manager should do next by calling decide_workflow_action.

Guidance:
- If the stage succeeded, the normal decision is "advance" to the next \
stage in the plan.
- If the stage failed, judge whether the error looks transient and worth \
retrying (a rate limit, a timeout, a momentary network error) versus \
serious enough that a human should look at it, versus unrecoverable for \
this project. A message that says the feature "is not implemented yet" or \
names a permanent configuration problem will not be fixed by retrying.
- Never choose "retry" once the project has already reached its retry \
limit — choose "escalate" instead. The retry count and limit are given to \
you; do the comparison yourself and honor it.
- Use "abort" only when the project itself cannot proceed no matter who \
looks at it (e.g. the underlying idea or input was invalid), not merely \
because a stage failed once.
"""

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


class ReasoningEngine:
    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._client = (
            anthropic.Anthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )

    def decide(
        self,
        *,
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

        prompt = self._build_prompt(
            phase=phase,
            stage=stage,
            job_status=job_status,
            error=error,
            retry_count=retry_count,
            max_retries=max_retries,
            next_stage=next_stage,
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                output_config={"effort": self._effort},
                system=_SYSTEM_PROMPT,
                tools=[_DECIDE_ACTION_TOOL],
                tool_choice={"type": "tool", "name": "decide_workflow_action"},
                messages=[{"role": "user", "content": prompt}],
            )
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
    def _build_prompt(
        *,
        phase: str,
        stage: str,
        job_status: str,
        error: str | None,
        retry_count: int,
        max_retries: int,
        next_stage: str | None,
    ) -> str:
        lines = [
            f"Phase: {phase}",
            f"Current stage: {stage}",
            f"Job outcome: {job_status}",
            f"Retries so far for this project: {retry_count} of {max_retries} allowed",
        ]
        if error:
            lines.append(f"Error: {error}")
        if next_stage:
            lines.append(f"Next stage in the plan if advancing: {next_stage}")
        else:
            lines.append(
                "This is the last stage in the plan; advancing marks the project complete."
            )
        lines.append("Decide the next action.")
        return "\n".join(lines)

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
