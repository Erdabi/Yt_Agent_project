"""ElevenLabs text-to-speech provider.

A real, working adapter against ElevenLabs' public API
(https://elevenlabs.io/docs/api-reference/), used by the Video Agent's
Voice Generation module
(services/agent_video/app/modules/voice_generation.py). Everything
specific to this one vendor — authentication, request construction, and
ElevenLabs-specific error handling — is encapsulated behind the same
`TTSProvider.synthesize(text) -> SynthesisResult` interface every other
tts provider implements, so nothing outside this file (Voice Generation,
the Asset Cache, config/providers.yaml) needs to know ElevenLabs exists
specifically. Unlike Runway's video generation
(libs/providers/video_gen/runway_provider.py), ElevenLabs' text-to-speech
endpoint is synchronous — one request returns the finished audio
directly, no task/poll/download flow needed.
"""

import base64
import time
from typing import Any

import requests

from .base import SynthesisResult, TTSProvider, WordTiming

#: ElevenLabs' commonly-used premade voice ("Rachel") — a reasonable
#: default when config/providers.yaml doesn't pin a specific voice_id.
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


class ElevenLabsAPIError(RuntimeError):
    """ElevenLabs rejected a request outright, or a request failed after
    exhausting its retry budget — distinct from a transient error (which
    this provider already retries internally; see `_request_with_retry`).
    Raised honestly rather than swallowed, so the Video Agent job fails
    and the Manager's existing retry-routing decides what happens next,
    the same as every other provider failure in this pipeline.
    """


class ElevenLabsProvider(TTSProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        if not api_key:
            raise ElevenLabsAPIError(
                "ElevenLabsProvider requires an API key — set the environment "
                "variable named by config/providers.yaml's tts.elevenlabs."
                "secret_ref (e.g. ELEVENLABS_API_KEY) before selecting "
                "'elevenlabs' as the active tts provider."
            )
        self._api_key = api_key
        self._base_url = config.get("base_url", "https://api.elevenlabs.io")
        self._voice_id = config.get("voice_id", _DEFAULT_VOICE_ID)
        self._model_id = config.get("model_id", "eleven_multilingual_v2")
        self._language_code = config.get("language_code")
        self._output_format = config.get("output_format", "mp3_44100_128")
        self._max_attempts = int(config.get("max_attempts", 3))
        self._retry_backoff_sec = float(config.get("retry_backoff_sec", 2))
        self._timeout_sec = float(config.get("timeout_sec", 60))

    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        resolved_voice_id = voice_id or self._voice_id
        model_id = kwargs.get("model_id", self._model_id)
        body: dict[str, Any] = {"text": text, "model_id": model_id, "output_format": self._output_format}
        if self._language_code:
            body["language_code"] = self._language_code

        response = self._request_with_retry(
            "POST",
            f"{self._base_url}/v1/text-to-speech/{resolved_voice_id}/with-timestamps",
            json=body,
        )
        data = self._json(response)

        audio_b64 = data.get("audio_base64")
        if not audio_b64:
            raise ElevenLabsAPIError(f"ElevenLabs response had no audio_base64: {list(data)!r}")
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except (ValueError, TypeError) as exc:
            raise ElevenLabsAPIError(f"ElevenLabs returned undecodable audio_base64: {exc}") from exc

        word_timings = self._word_timings_from_alignment(data.get("alignment"))

        return SynthesisResult(
            audio_bytes=audio_bytes,
            word_timings=word_timings,
            model=model_id,
            voice_id=resolved_voice_id,
            language=self._language_code or "en",
        )

    def cache_key_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "voice_id": self._voice_id,
            "model_id": self._model_id,
            "output_format": self._output_format,
        }
        if self._language_code:
            settings["language_code"] = self._language_code
        return settings

    # --- timing conversion -------------------------------------------------

    @staticmethod
    def _word_timings_from_alignment(alignment: dict[str, Any] | None) -> list[WordTiming] | None:
        """ElevenLabs reports per-*character* timestamps
        (`characters`/`character_start_times_seconds`/
        `character_end_times_seconds`); Voice Generation and Subtitle
        Generation both work in per-*word* timing, so this groups
        consecutive non-whitespace characters into words, taking the
        first character's start and the last character's end as that
        word's span.
        """
        if not alignment:
            return None
        characters = alignment.get("characters") or []
        starts = alignment.get("character_start_times_seconds") or []
        ends = alignment.get("character_end_times_seconds") or []
        if not characters or not (len(characters) == len(starts) == len(ends)):
            return None

        timings: list[WordTiming] = []
        current_word = ""
        word_start = 0.0
        word_end = 0.0
        for char, start, end in zip(characters, starts, ends):
            if char.isspace():
                if current_word:
                    timings.append(WordTiming(word=current_word, start_sec=word_start, end_sec=word_end))
                    current_word = ""
                continue
            if not current_word:
                word_start = start
            current_word += char
            word_end = end
        if current_word:
            timings.append(WordTiming(word=current_word, start_sec=word_start, end_sec=word_end))
        return timings or None

    # --- provider-specific error handling / retry --------------------------

    def _request_with_retry(self, method: str, url: str, *, json: dict) -> requests.Response:
        headers = {"xi-api-key": self._api_key, "Content-Type": "application/json"}
        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            try:
                response = requests.request(
                    method, url, headers=headers, json=json, timeout=self._timeout_sec
                )
            except requests.exceptions.RequestException as exc:
                # Covers connection errors and timeouts alike — both
                # transient, both worth retrying.
                if is_last_attempt:
                    raise ElevenLabsAPIError(
                        f"ElevenLabs {method} {url} failed after {attempt} attempts: {exc}"
                    ) from exc
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            # 429 (rate limit / concurrency limit) and 5xx (transient
            # server-side failure) are both explicitly worth retrying —
            # ElevenLabs' own docs recommend exponential backoff on 429.
            if response.status_code == 429 or response.status_code >= 500:
                if is_last_attempt:
                    raise ElevenLabsAPIError(
                        f"ElevenLabs {method} {url} returned {response.status_code} after "
                        f"{attempt} attempts: {response.text[:500]}"
                    )
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 400:
                # A 400/401/403/404 (bad request, bad auth, forbidden,
                # unknown voice) is permanent — retrying an identical
                # request won't fix it.
                raise ElevenLabsAPIError(
                    f"ElevenLabs {method} {url} returned {response.status_code}: {response.text[:500]}"
                )

            return response

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise ElevenLabsAPIError(
                f"ElevenLabs returned a non-JSON response ({response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
