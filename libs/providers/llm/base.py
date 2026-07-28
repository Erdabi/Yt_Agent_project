"""LLM provider interface.

A concrete implementation forces a model to call one caller-supplied
"tool" (a structured-output schema) and returns its parsed input, plus
which model actually answered and the token counts, so a caller can
track usage without knowing anything vendor-specific.

Every reasoning call site in this codebase routes through
`libs.providers.get_provider("llm")` — Research's `IdeaGenerator`/
`KnowledgeBuilder`, the Script Agent's `ScriptGenerator`/`ScriptReviewer`,
the Manager's reasoning engine, the Thumbnail Agent's
`ThumbnailConceptGenerator`, and the Quality Control Agent's reviewers —
none of them import a vendor SDK (`anthropic`, an Ollama client, or
otherwise) directly. Which concrete model answers is a
`config/providers.yaml` edit (or an `LLM_PROVIDER` env override), never a
code change — see `ollama_provider.py` (the local default) and
`anthropic_provider.py` (a swappable cloud alternative).
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
    #: True only when the provider actually exercised a live web search/
    #: fetch capability while producing this result (not merely whether
    #: the caller passed `enable_web_research=True` — a provider that has
    #: the capability may still not need it for a trivial prompt). Always
    #: False for a provider with no such capability at all. Lets a caller
    #: like `KnowledgeBuilder` honestly label a result that was answered
    #: from parametric knowledge rather than verified sources, without
    #: branching on which concrete provider is configured.
    used_web_research: bool = False


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
        enable_web_research: bool = False,
    ) -> LLMToolResult:
        """Force the model to call `tool` and return its structured
        input. `images` (PNG bytes), when given, are attached as visual
        context alongside `user_prompt` — e.g. the Quality Control
        Agent's `ThumbnailReviewer` reviewing the actual rendered
        thumbnail image, not just its generation metadata. A provider
        with no vision-capable model may honestly drop `images` (logging
        a warning) rather than fail outright — the same graceful-
        degradation contract `TTSProvider`/`word_timings` already
        establishes for a capability a given local model lacks.

        `enable_web_research`, when True, lets the provider ground its
        answer in live sources (server-side web search/fetch tools, when
        it has them) before finally calling `tool` — used only by
        Research's `KnowledgeBuilder`, whose whole job is producing
        verified facts with real citations, never invented ones. A
        provider with no such capability (e.g. a local model with no
        server-side tools) must still answer — best-effort, from its own
        knowledge — rather than raise; it is the caller's responsibility
        to treat an unverified package as such (see `KnowledgeBuilder`'s
        `supporting_notes` handling), not the provider's to refuse.
        Every multi-turn mechanics this requires (continuations, tool
        wiring) stays inside the provider — a caller only ever sees one
        `LLMToolResult` back, exactly like every other call.

        Must raise `LLMConfigError`/`LLMRefusalError`/`LLMResponseError`
        (never a bare/vendor-specific exception) on any failure.
        """
