"""LLM usage tracking — one `LLMUsageLog` row per API call (provider,
model, prompt version, input/output tokens, elapsed time, estimated
cost), stored against the project it was spent on, for future
analytics/optimization. See tracker.py's `track_llm_call` for the entry
point every Claude call site in this codebase wraps its call with.
"""

from .pricing import estimate_cost_usd
from .tracker import record_llm_usage, track_llm_call

__all__ = ["track_llm_call", "record_llm_usage", "estimate_cost_usd"]
