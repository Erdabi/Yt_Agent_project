"""The only `youtube` provider configured today (see
config/providers.yaml). Exists so the config-file switching mechanism
itself is fully real and testable (libs/providers/registry.py), without
fabricating an actual YouTube Data API integration, which is deferred to
Phase 1/3 (docs/architecture/06-roadmap.md).
"""

from typing import Any

from .base import YouTubePublisher


class StubYouTubePublisher(YouTubePublisher):
    def upload(self, file_path: str, metadata: dict[str, Any]) -> str:
        raise NotImplementedError(
            "No real YouTube publishing provider is configured. Add one "
            "under libs/providers/youtube/, register it in "
            "config/providers.yaml, and set youtube.active to its name "
            "(docs/architecture/06-roadmap.md, Phase 1/3)."
        )
