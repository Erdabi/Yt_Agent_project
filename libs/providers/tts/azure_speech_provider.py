"""Azure Speech text-to-speech provider — an honest stub.

Azure's Speech service TTS REST endpoint (`POST /cognitiveservices/v1`
on a `{resource_name}.cognitiveservices.azure.com` host, SSML request
body, `Authorization: Bearer <token>`) needs a resource-specific region/
host plus a short-lived access token exchange, not just a static API
key — wiring that up correctly (token acquisition/refresh, SSML
construction, and this vendor's own word-boundary/timing mechanism,
which isn't exposed the same way over plain REST as ElevenLabs' character
alignment) needs a real Azure resource to verify against, which this
codebase doesn't have. Kept an honest stub rather than a guessed
implementation that would look real but silently be wrong — the same
treatment `libs/providers/video_gen/invideo_provider.py` and
`veo_provider.py` get, and for the same underlying reason.

Registered in config/providers.yaml as a second `tts` option alongside
`elevenlabs`, satisfying "stubs for future TTS providers" — not because
this codebase needs multiple real TTS vendors today, but so a future
implementation has a real config slot (`tts.azure_speech`) to land in.

Wire up a real implementation here once Azure resource access exists:
authentication (`_headers`/token acquisition), request creation
(`_create_request`, building SSML), and Azure-specific error handling,
following the exact same shape as
`libs/providers/tts/elevenlabs_provider.py`'s `ElevenLabsProvider` — the
method name below is a placeholder for that future work, not a real
implementation.
"""

from typing import Any

from .base import SynthesisResult, TTSProvider


class AzureSpeechAPIError(RuntimeError):
    """Raised by a real Azure Speech implementation once one exists —
    kept as a named type now so callers that might someday catch it
    specifically don't need to change when this stub becomes real.
    """


class AzureSpeechProvider(TTSProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        self._api_key = api_key
        self._region = config.get("region", "eastus")
        self._voice_name = config.get("voice_name", "en-US-JennyNeural")

    def synthesize(self, text: str, *, voice_id: str | None = None, **kwargs: Any) -> SynthesisResult:
        raise NotImplementedError(
            "Azure Speech needs a real Azure resource (region + access-token "
            "exchange) to implement and verify against, which isn't available "
            "here (see this module's docstring). Once one exists: implement "
            "token acquisition, SSML request creation, and Azure-specific "
            "error handling in _create_request below following "
            "ElevenLabsProvider's shape, and flip tts.active to "
            "'azure_speech' in config/providers.yaml — no other file needs "
            "to change."
        )
