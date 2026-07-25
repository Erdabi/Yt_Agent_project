"""The only `image_gen` provider configured today (see
config/providers.yaml). Exists so the config-file switching mechanism
itself is fully real and testable (libs/providers/registry.py), without
fabricating an actual image-gen vendor integration, which is deferred to
Phase 1/4 (docs/architecture/06-roadmap.md).
"""

from typing import Any

from .base import ImageGenProvider


class StubImageGenProvider(ImageGenProvider):
    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "No real image generation provider is configured. Add one under "
            "libs/providers/image_gen/, register it in config/providers.yaml, "
            "and set image_gen.active to its name "
            "(docs/architecture/06-roadmap.md, Phase 1/4)."
        )
