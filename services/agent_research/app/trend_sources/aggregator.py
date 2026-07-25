"""Combines every configured trend source into one signal list for the
idea generator (../idea_generator.py). A source that isn't implemented
yet degrades the result, not the whole job — matches the documented
failure mode (docs/architecture/03-agent-responsibilities.md §3.2): "a
trend source being unreachable is non-fatal — the agent degrades to
fewer sources rather than failing the whole run."
"""

from libs.core.logging import get_logger

from .base import TrendSource
from .google_trends import GoogleTrendsSource
from .reddit import RedditSource
from .rss import RSSFeedSource
from .seed_list import SeedListTrendSource
from .youtube_trending import YouTubeTrendingSource

logger = get_logger(__name__)


class TrendAggregator:
    def __init__(self, sources: list[TrendSource] | None = None) -> None:
        self._sources = sources or [
            SeedListTrendSource(),
            YouTubeTrendingSource(),
            GoogleTrendsSource(),
            RedditSource(),
            RSSFeedSource(),
        ]

    def gather_signals(self, *, channel_niche: str, seed_topics: list[str] | None) -> list[str]:
        signals: list[str] = []
        for source in self._sources:
            try:
                signals.extend(source.fetch(channel_niche=channel_niche, seed_topics=seed_topics))
            except NotImplementedError as exc:
                logger.info("trend_source_unavailable", source=source.name, reason=str(exc))

        seen: set[str] = set()
        deduped: list[str] = []
        for signal in signals:
            if signal not in seen:
                seen.add(signal)
                deduped.append(signal)
        return deduped
