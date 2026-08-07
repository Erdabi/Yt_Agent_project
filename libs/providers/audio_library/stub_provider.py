"""The only `audio_library` provider configured today (see
config/providers.yaml). Exists so the config-file switching mechanism
itself is fully real and testable (libs/providers/registry.py), without
fabricating an actual sound-library vendor integration, which is
deferred to Phase 1 (docs/architecture/06-roadmap.md).
"""

from typing import Any

from ..base import StubProvider
from .base import AudioLibraryProvider


class StubAudioLibraryProvider(StubProvider, AudioLibraryProvider):
    unavailable_reason = (
        "No real audio library provider is configured. Add one under "
        "libs/providers/audio_library/, register it in "
        "config/providers.yaml, and set audio_library.active to its "
        "name (docs/architecture/06-roadmap.md, Phase 1)."
    )

    def search(self, description: str, **kwargs: Any) -> bytes:
        raise self._unavailable()
