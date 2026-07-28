"""Kokoro text-to-speech provider — a local, self-hosted, free-inference
alternative to ElevenLabs, for narration when no paid TTS vendor account
exists. Runs Kokoro-82M entirely on-device via its public `KPipeline`
API (https://github.com/hexgrad/kokoro, `pip install kokoro`): no network
call, no vendor account, no API key.

CAVEAT: the `kokoro` package is not installed in the environment this
provider was authored/verified in, so this targets `KPipeline`'s
documented, stable public surface (`KPipeline(lang_code=...)`, called
with text and returning a generator of `(graphemes, phonemes, audio)`
chunks — unchanged since the project's initial release) rather than
something inspected directly. Before relying on this in production,
confirm the installed version still exposes that exact surface — e.g.
`python -c "from kokoro import KPipeline; help(KPipeline.__call__)"` —
since a local pip package can move independently of this repo.

Kokoro's `KPipeline` reports audio per text *chunk* (its own internal
sentence/line splitting), not per word — there is no word-level alignment
API to translate into `WordTiming`, unlike ElevenLabs' character
timestamps. That is an explicitly supported configuration this codebase
already designs for: `SynthesisResult.word_timings=None` here, and
Subtitle Generation's/Voice Generation's existing fallback (an even split
of the real, measured audio duration across the segment's words —
services/agent_video/app/modules/{subtitle_generation,voice_generation}.py)
covers exactly this case, no different from Azure Speech's stub.
"""

import io
import os
import subprocess
import tempfile
import wave
from typing import Any

from .base import SynthesisResult, TTSProvider

#: Per-call budget for the ffmpeg transcode (WAV -> MP3) step below —
#: generous for what is always one short narration clip.
_FFMPEG_TIMEOUT_SEC = 60


class KokoroProviderError(RuntimeError):
    """Kokoro failed to load or synthesize — the `kokoro` package isn't
    installed, a voice pack couldn't be resolved/downloaded, or
    synthesis itself raised. Raised honestly rather than swallowed,
    matching every other TTS provider's error handling
    (elevenlabs_provider.py's `ElevenLabsAPIError`).
    """


class KokoroTTSProvider(TTSProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        #: Kokoro's language codes: 'a' American English, 'b' British
        #: English, 'e' Spanish, 'f' French, 'h' Hindi, 'i' Italian,
        #: 'j' Japanese, 'p' Brazilian Portuguese, 'z' Mandarin Chinese.
        self._lang_code = config.get("lang_code", "a")
        #: A Kokoro voice pack name (e.g. "af_heart", "am_michael",
        #: "bf_emma") — downloaded from HuggingFace on first use of that
        #: voice and cached locally by the `kokoro` package itself.
        self._voice = config.get("voice", "af_heart")
        self._speed = float(config.get("speed", 1.0))
        self._sample_rate = int(config.get("sample_rate", 24000))
        #: How `KPipeline` splits input text into synthesis chunks —
        #: Kokoro's own default; overridable per config/providers.yaml
        #: if a channel's narration style needs different chunking.
        self._split_pattern = config.get("split_pattern", r"\n+")
        #: Lazy: constructing `KPipeline` loads model weights, so a
        #: `KokoroTTSProvider` instantiated but never used (e.g. because
        #: `get_provider("tts")` was called for a different capability
        #: check) never pays that cost.
        self._pipeline: Any = None

    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        voice = voice_id or self._voice
        speed = float(kwargs.get("speed", self._speed))

        try:
            pipeline = self._get_pipeline()
            generator = pipeline(text, voice=voice, speed=speed, split_pattern=self._split_pattern)
            chunks = [audio for _, _, audio in generator]
        except KokoroProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - every synthesis failure must surface honestly
            raise KokoroProviderError(f"Kokoro synthesis failed: {exc}") from exc

        if not chunks:
            raise KokoroProviderError(f"Kokoro produced no audio for voice {voice!r}")

        audio_bytes = self._encode_mp3(chunks)

        return SynthesisResult(
            audio_bytes=audio_bytes,
            word_timings=None,
            model="kokoro-82M",
            voice_id=voice,
            language=self._lang_code,
        )

    def cache_key_settings(self) -> dict[str, Any]:
        return {"voice": self._voice, "lang_code": self._lang_code, "speed": self._speed}

    # --- model loading -------------------------------------------------------

    def _get_pipeline(self) -> Any:
        if self._pipeline is None:
            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise KokoroProviderError(
                    "the 'kokoro' package is not installed — install it (pip install kokoro) "
                    "to use KokoroTTSProvider as the active tts provider"
                ) from exc
            try:
                self._pipeline = KPipeline(lang_code=self._lang_code)
            except Exception as exc:  # noqa: BLE001 - model load failures must surface honestly
                raise KokoroProviderError(f"failed to load Kokoro pipeline: {exc}") from exc
        return self._pipeline

    # --- audio encoding --------------------------------------------------------

    def _encode_mp3(self, chunks: list[Any]) -> bytes:
        """Concatenates Kokoro's per-chunk float32 audio arrays into one
        clip and transcodes to MP3 (the format every other TTS provider
        in this codebase returns, and what Voice Generation's Asset
        Cache stores under — see voice_generation.py's hardcoded
        `extension="mp3"`). Goes through a real WAV intermediate + the
        `ffmpeg` binary already required by this service's compositor
        (libs/providers/editor/ffmpeg_provider.py) rather than adding a
        new audio-encoding dependency.
        """
        import numpy as np

        samples = np.concatenate([np.asarray(chunk, dtype=np.float32).reshape(-1) for chunk in chunks])
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate)
            wav_file.writeframes(pcm16.tobytes())

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_tmp:
            wav_tmp.write(wav_buffer.getvalue())
            wav_path = wav_tmp.name
        mp3_path = f"{wav_path[:-4]}.mp3"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-codec:a", "libmp3lame", "-qscale:a", "2", mp3_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_FFMPEG_TIMEOUT_SEC,
            )
            if result.returncode != 0:
                raise KokoroProviderError(
                    f"ffmpeg failed to encode Kokoro audio to mp3: "
                    f"{result.stderr.decode('utf-8', errors='replace')}"
                )
            with open(mp3_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(wav_path)
            if os.path.exists(mp3_path):
                os.unlink(mp3_path)
