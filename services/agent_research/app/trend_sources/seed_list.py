"""The one trend source that's real today: a configured/passed seed-topic
list rather than live scraping. This is the Phase 1 approach documented in
docs/architecture/06-roadmap.md ("manual/seed topic list ... live trend
scraping deferred to Phase 4") — it always works, has no external
dependency, and is exactly what feeds a human-submitted goal (the goal
itself is passed in as a seed topic) or a channel's own configured
interests (`Channel.persona_config["seed_topics"]`).
"""

from .base import TrendSource


class SeedListTrendSource(TrendSource):
    name = "seed_list"

    def fetch(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        return list(seed_topics or [])
