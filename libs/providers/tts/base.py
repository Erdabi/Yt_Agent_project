"""Text-to-speech provider interface.

A concrete implementation synthesizes one script segment's audio for the
Video Agent's Voice Generation module
(services/agent_video/app/modules/voice_generation.py). No real
implementation exists yet — see stub_provider.py and
docs/architecture/06-roadmap.md, Phase 1.
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


class TTSProvider(Provider):
    @abstractmethod
    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        """Synthesize `text` and return the audio plus, when available,
        per-word timing.
        """
