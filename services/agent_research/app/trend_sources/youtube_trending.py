"""Live YouTube Trending feed. Not implemented yet — see
docs/architecture/06-roadmap.md, Phase 4 ("Live trend scraping ...
replacing the Phase 1 seed-list approach"). Raises `NotImplementedError`
rather than fabricating trend data; the aggregator (aggregator.py)
treats that as non-fatal.
"""

from .base import TrendSource


class YouTubeTrendingSource(TrendSource):
    name = "youtube_trending"

    def fetch(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        raise NotImplementedError(
            "YouTube Trending integration lands in Phase 4 of the roadmap "
            "(docs/architecture/06-roadmap.md)."
        )
