"""Records usage for every LLM call — provider, model, prompt version,
token counts, elapsed time, estimated cost — so the system has an actual
history to analyze/optimize against instead of no record at all.

`track_llm_call` is the entry point every call site uses: it times the
call, captures whatever token counts the caller reports, and writes one
`LLMUsageLog` row on exit — success or failure alike, since a failed call
still took time and (usually) still consumed input tokens. Modeled on
`libs.agents.base.BaseAgent._notify_manager`'s "never allowed to raise"
rule: a usage-tracking failure (e.g. a momentary DB hiccup) must never be
able to break the LLM call it's merely observing, so `record_llm_usage`
catches and logs instead of propagating.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.usage import LLMUsageLog

from .pricing import estimate_cost_usd

logger = get_logger(__name__)


def record_llm_usage(
    *,
    project_id: str | None,
    agent_name: str,
    call_site: str,
    provider: str,
    model: str,
    prompt_name: str | None,
    prompt_version: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    elapsed_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        cost = estimate_cost_usd(model, input_tokens, output_tokens)
        with sync_session_scope() as session:
            session.add(
                LLMUsageLog(
                    project_id=UUID(project_id) if project_id else None,
                    agent_name=agent_name,
                    call_site=call_site,
                    provider=provider,
                    model=model,
                    prompt_name=prompt_name,
                    prompt_version=prompt_version,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    elapsed_ms=elapsed_ms,
                    cost_estimate_usd=cost,
                    success=success,
                    error=error,
                )
            )
    except Exception as exc:
        logger.error("llm_usage_record_failed", call_site=call_site, error=str(exc))


@contextmanager
def track_llm_call(
    *,
    project_id: str | None,
    agent_name: str,
    call_site: str,
    model: str,
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    provider: str = "anthropic",
) -> Iterator[dict[str, Any]]:
    """Wrap one LLM API call:

        with track_llm_call(
            project_id=project_id, agent_name="research", call_site="idea_generator.generate",
            model=self._model, prompt_name="generate_ideas", prompt_version=resolved_version,
        ) as usage:
            response = self._client.messages.create(...)
            usage["input_tokens"] = response.usage.input_tokens
            usage["output_tokens"] = response.usage.output_tokens

    `usage` starts empty; populate `input_tokens`/`output_tokens` after a
    successful call. Recorded on exit regardless of whether the block
    raised — a failed call still gets a row (`success=False`, whatever
    token counts were set before the exception, `error` set to the
    exception message), so a string of failures is visible in the usage
    log, not just successes.
    """
    start = time.monotonic()
    usage: dict[str, Any] = {"input_tokens": None, "output_tokens": None}
    error: str | None = None
    try:
        yield usage
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        record_llm_usage(
            project_id=project_id,
            agent_name=agent_name,
            call_site=call_site,
            provider=provider,
            model=model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            elapsed_ms=elapsed_ms,
            success=error is None,
            error=error,
        )
