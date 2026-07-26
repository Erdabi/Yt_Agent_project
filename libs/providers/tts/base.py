"""Text-to-speech provider interface.

A concrete implementation synthesizes one script segment's audio for the
Video Agent's Voice Generation module
(services/agent_video/app/modules/voice_generation.py). ElevenLabs
(elevenlabs_provider.py) is a real, working implementation; Azure Speech
(azure_speech_provider.py) remains an honest stub — see each module's
own docstring.
"""

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from libs.providers.base import Provider


@dataclass(frozen=True)
class WordTiming:
    word: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class SynthesisResult:
    audio_bytes: bytes
    #: Per-word timing, when the provider supports it (e.g. ElevenLabs'
    #: character/word timestamps). `None` when it doesn't — the Video
    #: Agent's Subtitle Generation module falls back to evenly
    #: distributing the known audio duration across words instead of
    #: requiring this. Translating a specific vendor's timestamp API into
    #: this shape is that vendor's concrete provider class's job, not any
    #: caller's — the same reason this interface exists at all.
    word_timings: list[WordTiming] | None = None
    #: What actually produced this audio — reported back by the provider
    #: itself (never guessed by a caller) so Voice Generation can persist
    #: it verbatim onto `Voiceover`/`Asset.metadata_` without knowing
    #: which vendor is configured.
    model: str | None = None
    voice_id: str | None = None
    #: No per-channel/per-project language configuration exists yet
    #: (same reasoning as `AssetCacheKey.language` in ../../../services/
    #: agent_video/app/asset_cache.py) — defaults to a constant baseline
    #: rather than `None`.
    language: str = "en"


class TTSProvider(Provider):
    @abstractmethod
    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        """Synthesize `text` and return the audio plus, when available,
        per-word timing.
        """

    def cache_key_settings(self) -> dict[str, Any]:
        """Settings that affect this provider's output and must be part
        of the Asset Cache key — e.g. a configured default voice/model —
        so a config change doesn't wrongly reuse audio generated under a
        different configuration. Empty by default; a concrete provider
        overrides this when it has such settings (see
        `ElevenLabsProvider`). Kept generic here so Voice Generation can
        call it without knowing anything vendor-specific.
        """
        return {}
