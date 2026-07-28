"""Per-model USD pricing, for estimating the cost of one LLM call from its
token counts. A snapshot, not a live lookup — see the claude-api skill's
model table for the source and refresh it there if pricing changes.
"""

#: (input $ / 1M tokens, output $ / 1M tokens). Snapshot cached 2026-06-24.
#: Sonnet 5 uses its standard (non-intro) rate: this codebase defaults to
#: Opus 5 (libs/core/config.py's ANTHROPIC_MODEL), and the intro rate is
#: time-boxed, so pricing an occasional Sonnet call at the promotional
#: rate would understate steady-state cost.
_PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Local inference (libs/providers/llm/ollama_provider.py) — genuinely
    # $0, not "unknown"; listed explicitly rather than left to fall
    # through to the None-for-unknown-model branch below, so a usage-log
    # row for a local call reads as "$0.00 confirmed" rather than
    # "cost not estimated."
    "qwen3:32b": (0.0, 0.0),
}


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Returns `None` — never a guessed number — for an unknown model or
    missing token counts, rather than silently reporting $0 or a wrong
    price.
    """
    pricing = _PRICING_PER_MILLION_TOKENS.get(model)
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    input_price, output_price = pricing
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
