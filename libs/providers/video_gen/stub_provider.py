"""The only `video_gen` provider configured today (see
config/providers.yaml). Exists so the config-file switching mechanism
itself — load YAML, resolve the active name, dynamically import and
instantiate the class — is fully real and testable (libs/providers/registry.py),
without fabricating an actual AI video-generation integration, which is
deferred to Phase 2+ (docs/architecture/06-roadmap.md).
"""

from typing import Any

from ..base import StubProvider
from .base import VideoGenProvider


class StubVideoGenProvider(StubProvider, VideoGenProvider):
    unavailable_reason = (
        "No real video generation provider is configured. Add one under "
        "libs/providers/video_gen/, register it in config/providers.yaml, "
        "and set video_gen.active to its name "
        "(docs/architecture/06-roadmap.md, Phase 2+)."
    )

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        raise self._unavailable()
