"""Video generation provider interface.

A concrete implementation generates an AI video clip from a prompt, for
use as a shot in the Video Agent's storyboard/assembly modules
(services/agent_video/app/modules/). AI b-roll generation is Phase 2+ per
docs/architecture/06-roadmap.md — the MVP relies primarily on stock
footage, so no real implementation exists yet (see stub_provider.py).
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class VideoGenProvider(Provider):
    @abstractmethod
    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        """Generate a video clip for `prompt` and return its raw bytes."""
