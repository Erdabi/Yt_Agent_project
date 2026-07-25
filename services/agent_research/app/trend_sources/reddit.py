"""Reddit (configured subreddits per channel niche). Not implemented yet
— see docs/architecture/06-roadmap.md, Phase 4. Raises
`NotImplementedError` rather than fabricating trend data; the aggregator
(aggregator.py) treats that as non-fatal.
"""

from .base import TrendSource


class RedditSource(TrendSource):
    name = "reddit"

    def fetch(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        raise NotImplementedError(
            "Reddit integration lands in Phase 4 of the roadmap "
            "(docs/architecture/06-roadmap.md)."
        )
