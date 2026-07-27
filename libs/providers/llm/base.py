"""LLM provider interface.

A concrete implementation forces a model to call one caller-supplied
"tool" (a structured-output schema) and returns its parsed input, plus
which model actually answered and the token counts, so a caller can
track usage without knowing anything vendor-specific.

Distinct from every other agent's own direct `anthropic.Anthropic()` SDK
call (Research's `IdeaGenerator`, the Script Agent's `ScriptGenerator`,
the Manager's reasoning engine, the Thumbnail Agent's
`ThumbnailConceptGenerator`) — those predate this capability and stay as
they are (retrofitting them is a separate, unrequested change). The
Quality Control Agent (services/agent_qa/app/) is the first consumer that
routes its LLM reasoning through `libs.providers.get_provider("llm")`
rather than a hardcoded `anthropic` import, so its reviewers' model
choice is a `config/providers.yaml` edit like every other capability in
this codebase, not a code change.
"""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from libs.providers.base import Provider


class LLMProviderError(RuntimeError):
    """Base for every LLM provider failure. A caller never needs to catch
    a vendor-specific SDK exception — only one of these.
    """


class LLMConfigError(LLMProviderError):
    """The provider isn't usable as configured (e.g. no API key) —
    retrying the identical call won't help.
    """


class LLMRefusalError(LLMProviderError):
    """The model declined to answer at all."""


class LLMResponseError(LLMProviderError):
    """The call reached the vendor but didn't produce a usable result —
    a transport/API failure, or a response that never called the forced
    tool.
    """


@dataclass(frozen=True)
class LLMToolCall:
    """A caller-defined structured-output contract — the provider-
    agnostic shape every concrete provider translates into its own
    "force this tool" mechanism (Anthropic's `tool_choice`, or whatever
    an eventual OpenAI/other provider uses instead).
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LLMToolResult:
    #: The tool's parsed arguments, matching `LLMToolCall.input_schema`.
    tool_input: dict[str, Any]
    #: Self-reported by the provider (never guessed by a caller) — same
    #: reporting-back convention as `libs.providers.tts.base.SynthesisResult`.
    model: str
    provider_name: str
    input_tokens: int | None
    output_tokens: int | None


class LLMProvider(Provider):
    @property
    @abstractmethod
    def model(self) -> str:
        """The concrete model this provider is configured to use — known
        before a call is made, so a caller can pass it to
        `libs.llm_usage.track_llm_call` without waiting for a response.
        """

    @abstractmethod
    def generate_tool_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool: LLMToolCall,
        images: list[bytes] | None = None,
    ) -> LLMToolResult:
        """Force the model to call `tool` and return its structured
        input. `images` (PNG bytes), when given, are attached as visual
        context alongside `user_prompt` — e.g. the Quality Control
        Agent's `ThumbnailReviewer` reviewing the actual rendered
        thumbnail image, not just its generation metadata. Must raise
        `LLMConfigError`/`LLMRefusalError`/`LLMResponseError` (never a
        bare/vendor-specific exception) on any failure.
        """
