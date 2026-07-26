"""The default `tts` provider (see config/providers.yaml's `active: stub`)
— stays the default so nothing calls a paid vendor without a real API
key configured. A real implementation exists alongside this one
(elevenlabs_provider.py); flip `tts.active` to `elevenlabs` (or set
`TTS_PROVIDER=elevenlabs`) once `ELEVENLABS_API_KEY` is set.
"""

from typing import Any

from .base import SynthesisResult, TTSProvider


class StubTTSProvider(TTSProvider):
    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        raise NotImplementedError(
            "No real text-to-speech provider is configured. Add one under "
            "libs/providers/tts/, register it in config/providers.yaml, and "
            "set tts.active to its name (docs/architecture/06-roadmap.md, Phase 1)."
        )
