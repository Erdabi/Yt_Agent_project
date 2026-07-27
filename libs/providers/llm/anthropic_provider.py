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
    ) -> LLMToolResult:
        if self._client is None:
            raise LLMConfigError("ANTHROPIC_API_KEY is not configured")

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
