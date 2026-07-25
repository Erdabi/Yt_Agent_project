"""Text-to-speech provider interface.

A concrete implementation synthesizes one script segment's audio for the
Video Agent's voiceover module
(services/agent_video/app/modules/voiceover.py). No real implementation
exists yet — see stub_provider.py and
docs/architecture/06-roadmap.md, Phase 1.
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class TTSProvider(Provider):
    @abstractmethod
    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> bytes:
        """Synthesize `text` and return raw audio bytes."""
