"""A real, working `llm` provider backed directly by the Anthropic API.

Unlike every capability that stays `stub` until a real vendor API key is
configured, this one is the default (`active: anthropic` in
config/providers.yaml) — Anthropic is already a hard requirement
elsewhere in this system (the Manager's reasoning engine, Research/Script
Agent calls), so defaulting here doesn't introduce a new paid-vendor
dependency; it just makes an already-required one swappable through this
same registry mechanism.
"""

import base64
from typing import Any

import anthropic

from libs.core.config import get_settings

from .base import (
    LLMConfigError,
    LLMProvider,
    LLMRefusalError,
    LLMResponseError,
    LLMToolCall,
    LLMToolResult,
)

_IMAGE_MEDIA_TYPE = "image/png"

#: Server-side tools — Anthropic executes these itself within the same
#: turn; there is no client-side function to implement for either. Only
#: used when a caller sets `enable_web_research=True` (Research's
#: `KnowledgeBuilder` — see generate_tool_call's docstring in base.py).
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

#: How many times to resend a `pause_turn`-interrupted research turn
#: before giving up. Bounds worst-case latency/cost on a topic that keeps
#: triggering long server-tool turns.
_MAX_CONTINUATIONS = 3


class AnthropicLLMProvider(LLMProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        settings = get_settings()
        self._model = config.get("model") or settings.anthropic_model
        self._effort = config.get("effort") or settings.anthropic_effort
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else None

    @property
    def model(self) -> str:
        return self._model

    def generate_tool_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool: LLMToolCall,
        images: list[bytes] | None = None,
        enable_web_research: bool = False,
    ) -> LLMToolResult:
        if self._client is None:
            raise LLMConfigError("ANTHROPIC_API_KEY is not configured")

        if enable_web_research:
            return self._generate_with_web_research(
                system_prompt=system_prompt, user_prompt=user_prompt, tool=tool
            )

        tool_schema = {
            "name": tool.name,
            "description": tool.description,
            "strict": True,
            "input_schema": tool.input_schema,
        }
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                output_config={"effort": self._effort},
                system=system_prompt,
                tools=[tool_schema],
                tool_choice={"type": "tool", "name": tool.name},
                messages=[{"role": "user", "content": _build_content(user_prompt, images)}],
            )
        except anthropic.APIError as exc:
            raise LLMResponseError(f"Claude API call failed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefusalError("Claude declined to respond")

        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            raise LLMResponseError(
                f"Claude did not call {tool.name!r} (stop_reason={response.stop_reason})"
            )

        return LLMToolResult(
            tool_input=tool_use.input,
            model=self._model,
            provider_name="AnthropicLLMProvider",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def _generate_with_web_research(
        self, *, system_prompt: str, user_prompt: str, tool: LLMToolCall
    ) -> LLMToolResult:
        """The research-grounded path: "verified facts" and real
        citations require Claude to actually look something up, not
        recall it from parametric memory. So this includes Anthropic's
        server-side `web_search`/`web_fetch` tools alongside the
        structured-output tool, and — critically — does **not** force
        `tool_choice` the way the plain path above does. Forcing
        `tool_choice` to `tool.name` would require Claude's very first
        action to be that tool call, leaving no room to search the web
        first. Leaving `tool_choice` at its default ("auto") lets Claude
        call web_search/web_fetch as many times as the topic needs before
        finally calling `tool` once it actually has something to report.

        That freedom has a real cost: a long research turn can pause
        mid-way with `stop_reason: "pause_turn"` rather than finishing.
        This resends the paused conversation with a bounded number of
        continuations, per Anthropic's documented pattern, rather than
        treating a pause as a failure. Token usage is summed across every
        turn this takes — the caller wraps this whole method in one
        `track_llm_call`, so it gets one usage-log row for the complete
        (possibly multi-turn) operation rather than one per HTTP call.
        """
        tool_schema = {
            "name": tool.name,
            "description": tool.description,
            "strict": True,
            "input_schema": tool.input_schema,
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        total_input_tokens = 0
        total_output_tokens = 0
        used_web_research = False

        for _ in range(_MAX_CONTINUATIONS + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=8192,
                    output_config={"effort": self._effort},
                    system=system_prompt,
                    tools=[_WEB_SEARCH_TOOL, _WEB_FETCH_TOOL, tool_schema],
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise LLMResponseError(f"Claude API call failed: {exc}") from exc

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            if any(
                block.type in ("server_tool_use", "web_search_tool_result", "web_fetch_tool_result")
                for block in response.content
            ):
                used_web_research = True

            if response.stop_reason == "refusal":
                raise LLMRefusalError("Claude declined to respond")

            tool_use = next(
                (
                    block
                    for block in response.content
                    if block.type == "tool_use" and block.name == tool.name
                ),
                None,
            )
            if tool_use is not None:
                return LLMToolResult(
                    tool_input=tool_use.input,
                    model=self._model,
                    provider_name="AnthropicLLMProvider",
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    used_web_research=used_web_research,
                )

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue

            raise LLMResponseError(
                f"Claude did not call {tool.name!r} (stop_reason={response.stop_reason})"
            )

        raise LLMResponseError(
            f"Claude did not finish researching within {_MAX_CONTINUATIONS} continuations"
        )


def _build_content(user_prompt: str, images: list[bytes] | None) -> str | list[dict]:
    if not images:
        return user_prompt
    content: list[dict] = [{"type": "text", "text": user_prompt}]
    for image_bytes in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _IMAGE_MEDIA_TYPE,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            }
        )
    return content
