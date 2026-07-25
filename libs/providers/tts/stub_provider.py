"""The only `tts` provider configured today (see config/providers.yaml).
Exists so the config-file switching mechanism itself is fully real and
testable (libs/providers/registry.py), without fabricating an actual TTS
vendor integration, which is deferred to Phase 1
(docs/architecture/06-roadmap.md).
"""

from typing import Any

from .base import TTSProvider


class StubTTSProvider(TTSProvider):
    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "No real text-to-speech provider is configured. Add one under "
            "libs/providers/tts/, register it in config/providers.yaml, and "
            "set tts.active to its name (docs/architecture/06-roadmap.md, Phase 1)."
        )
