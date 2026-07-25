"""Trend source interface.

Each source contributes raw trend "signals" (topic phrases, headlines,
search terms) that feed the Research Agent's idea-generation prompt
(../idea_generator.py) — it decides what to do with them, not the source
itself. A source that can't produce signals (no live credentials, an
unimplemented connector) raises `NotImplementedError`; the aggregator
(aggregator.py) treats that as non-fatal and degrades to fewer sources
rather than failing the whole research run
(docs/architecture/03-agent-responsibilities.md §3.2's documented failure
mode).
"""

from abc import ABC, abstractmethod


class TrendSource(ABC):
    #: Short, stable identifier used in logs when a source is unavailable.
    name: str

    @abstractmethod
    def fetch(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        """Return raw trend signal strings relevant to `channel_niche`.
        `seed_topics`, if given, are topics the caller already wants
        considered (e.g. a human-submitted goal) — a source may use them
        to focus its search, or ignore them entirely.
        """
