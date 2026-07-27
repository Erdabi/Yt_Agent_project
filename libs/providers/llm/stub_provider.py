"""An honest stub for the `llm` capability — exists so the config-file
switching mechanism itself is fully real and testable
(libs/providers/registry.py) independent of which provider is actually
active. `config/providers.yaml` defaults `llm.active` to `anthropic`
(see anthropic_provider.py's own docstring for why that default is safe
here, unlike every other capability's `stub` default); this exists for
completeness and as the pattern for a future non-Anthropic provider.
"""

from .base import LLMProvider, LLMToolCall, LLMToolResult


class StubLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "stub"

    def generate_tool_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool: LLMToolCall,
        images: list[bytes] | None = None,
    ) -> LLMToolResult:
        raise NotImplementedError(
            "No real LLM provider is configured. Add one under libs/providers/llm/, "
            "register it in config/providers.yaml, and set llm.active to its name."
        )
