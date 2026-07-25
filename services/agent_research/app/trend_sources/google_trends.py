"""Google Trends. Not implemented yet — see
docs/architecture/06-roadmap.md, Phase 4. Raises `NotImplementedError`
rather than fabricating trend data; the aggregator (aggregator.py) treats
that as non-fatal.
"""

from .base import TrendSource


class GoogleTrendsSource(TrendSource):
    name = "google_trends"

    def fetch(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        raise NotImplementedError(
            "Google Trends integration lands in Phase 4 of the roadmap "
            "(docs/architecture/06-roadmap.md)."
        )
