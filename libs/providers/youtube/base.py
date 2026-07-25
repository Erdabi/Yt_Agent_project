"""YouTube publishing provider interface.

There is only one real YouTube, so this isn't "swappable" in the sense
video/TTS/image-gen providers are — but it stays behind the same
config-driven interface so the Publisher Agent (services/agent_publisher)
can point at a stub/test double in local dev without touching the real
Data API and its quota, exactly like every other capability here. No real
implementation exists yet — see stub_provider.py and
docs/architecture/06-roadmap.md, Phase 1/3.
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class YouTubePublisher(Provider):
    @abstractmethod
    def upload(self, file_path: str, metadata: dict[str, Any]) -> str:
        """Upload the video at `file_path` with `metadata` (title,
        description, tags, etc.) and return the resulting YouTube video id.
        """
