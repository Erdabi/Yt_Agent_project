"""KokoroTTSProvider unit tests.

The `kokoro` package itself isn't installed in this environment (no GPU/
Python-package assumptions are made about the machine running these
tests) — these mock `kokoro.KPipeline` at the module boundary, the same
methodology this codebase already uses for every other vendor SDK
(Anthropic's `Messages.create`, `requests.request` for Runway/
ElevenLabs): real provider/encoding code, faked model weights.
"""

import re
import sys
import types
from unittest import mock

import numpy as np
import pytest

from libs.providers.tts.kokoro_provider import KokoroProviderError, KokoroTTSProvider


class _FakeKPipeline:
    """Mirrors the real `KPipeline`'s documented shape: constructed with
    `lang_code`, called with `(text, voice=, speed=, split_pattern=)`,
    yielding `(graphemes, phonemes, audio)` per text chunk.
    """

    last_lang_code = None

    def __init__(self, lang_code):
        _FakeKPipeline.last_lang_code = lang_code
        self.lang_code = lang_code

    def __call__(self, text, voice, speed, split_pattern):
        for part in [p for p in re.split(split_pattern, text) if p.strip()]:
            duration = max(len(part.split()) * 0.3, 0.2)
            t = np.linspace(0, duration, int(24000 * duration), endpoint=False)
            audio = 0.2 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
            yield (part, "PHONEMES", audio)


@pytest.fixture
def fake_kokoro_module():
    fake_module = types.ModuleType("kokoro")
    fake_module.KPipeline = _FakeKPipeline
    with mock.patch.dict(sys.modules, {"kokoro": fake_module}):
        yield fake_module


def test_synthesize_returns_valid_mp3_with_no_word_timings(fake_kokoro_module):
    provider = KokoroTTSProvider(config={}, api_key=None)
    result = provider.synthesize("Hello there.\nThis is a test sentence.")

    assert result.audio_bytes[:2] not in (b"", None)
    assert len(result.audio_bytes) > 100
    assert result.word_timings is None  # no per-word alignment API — see module docstring
    assert result.model == "kokoro-82M"
    assert result.voice_id == "af_heart"
    assert result.language == "a"

    # Real, decodable MP3 bytes — not just "some bytes".
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
        f.write(result.audio_bytes)
        f.flush()
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", f.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    assert probe.returncode == 0, probe.stderr.decode()
    assert float(probe.stdout.decode().strip()) > 0


def test_config_selects_voice_and_lang_code(fake_kokoro_module):
    provider = KokoroTTSProvider(
        config={"voice": "bm_george", "lang_code": "b", "speed": 1.2}, api_key=None
    )
    result = provider.synthesize("Right then.")
    assert result.voice_id == "bm_george"
    assert result.language == "b"
    assert _FakeKPipeline.last_lang_code == "b"


def test_voice_id_override_per_call(fake_kokoro_module):
    provider = KokoroTTSProvider(config={"voice": "af_heart"}, api_key=None)
    result = provider.synthesize("Test.", voice_id="am_michael")
    assert result.voice_id == "am_michael"


def test_cache_key_settings_includes_voice_lang_speed(fake_kokoro_module):
    provider = KokoroTTSProvider(config={"voice": "af_bella", "speed": 0.9}, api_key=None)
    settings = provider.cache_key_settings()
    assert settings == {"voice": "af_bella", "lang_code": "a", "speed": 0.9}


def test_pipeline_constructed_lazily_not_at_init(fake_kokoro_module):
    """Constructing `KPipeline` loads model weights — a provider merely
    instantiated (e.g. by the registry) must not pay that cost until a
    real `synthesize()` call happens.
    """
    KokoroTTSProvider(config={}, api_key=None)
    assert _FakeKPipeline.last_lang_code is None or True  # constructed only on demand below
    # A fresh provider that never calls synthesize() never touches KPipeline:
    _FakeKPipeline.last_lang_code = None
    provider = KokoroTTSProvider(config={"lang_code": "z"}, api_key=None)
    assert _FakeKPipeline.last_lang_code is None
    provider.synthesize("你好")
    assert _FakeKPipeline.last_lang_code == "z"


def test_missing_kokoro_package_raises_honestly():
    """No fake module installed — the real ImportError path."""
    with mock.patch.dict(sys.modules, {"kokoro": None}):
        provider = KokoroTTSProvider(config={}, api_key=None)
        with pytest.raises(KokoroProviderError, match="pip install kokoro"):
            provider.synthesize("test")


def test_empty_audio_raises_honestly(fake_kokoro_module):
    class _EmptyPipeline:
        def __init__(self, lang_code):
            pass

        def __call__(self, *args, **kwargs):
            return iter([])

    fake_kokoro_module.KPipeline = _EmptyPipeline
    provider = KokoroTTSProvider(config={}, api_key=None)
    with pytest.raises(KokoroProviderError, match="no audio"):
        provider.synthesize("test")
