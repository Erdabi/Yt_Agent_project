"""Image generation provider interface.

A concrete implementation generates a still image from a prompt, used by
the Video Agent's storyboard module (for AI-image shots) and thumbnail
module (for thumbnail candidates) —
services/agent_video/app/modules/{storyboard,thumbnail}.py. No real
implementation exists yet — see stub_provider.py and
docs/architecture/06-roadmap.md, Phase 1/4.
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class ImageGenProvider(Provider):
    @abstractmethod
    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        """Generate an image for `prompt` and return its raw bytes."""
