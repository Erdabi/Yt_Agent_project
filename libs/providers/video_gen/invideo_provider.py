"""InVideo AI video generation provider — an honest stub.

InVideo AI does not currently publish a stable, public REST API contract
for programmatic text-to-video generation that this codebase could
verify against: its own developer documentation domain
(`invideo.io/developer/docs`) returns 404, `docs.invideo.io` does not
resolve, and every third-party source found only mentions "generate an
API key in Settings > Developers > API Keys" without a documented
endpoint, request/response shape, or auth header — the same due
diligence this codebase already applies before claiming a real vendor
integration exists (see e.g. libs/providers/image_gen/stub_provider.py).
Rather than fabricate endpoint paths and field names that would look
real but silently be wrong, this stays an honest stub — the same
treatment `config/providers.yaml`'s Google Veo entry gets, and for the
same underlying reason (no verifiable public API surface today).

Wire up a real implementation here once InVideo publishes a documented
API: authentication (`_headers`), request creation (`_create_task`),
polling (`_poll_until_complete`), downloading (`_download`), and
InVideo-specific error handling, following the exact same shape as
`libs/providers/video_gen/runway_provider.py`'s `RunwayProvider` — the
method names below are placeholders for that future work, not a real
implementation.
"""

from typing import Any

from .base import VideoGenProvider


class InVideoAPIError(RuntimeError):
    """Raised by a real InVideo implementation once one exists — kept as
    a named type now so callers that might someday catch it specifically
    don't need to change when this stub becomes real.
    """


class InVideoProvider(VideoGenProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        self._api_key = api_key
        self._base_url = config.get("base_url", "https://api.invideo.io")
        self._poll_interval_sec = float(config.get("poll_interval_sec", 5))
        self._poll_timeout_sec = float(config.get("poll_timeout_sec", 300))

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "InVideo AI has no verified, publicly-documented REST API contract "
            "to implement against today (see this module's docstring). Once "
            "InVideo publishes one: add a secret_ref for its API key, fill in "
            "_headers/_create_task/_poll_until_complete/_download below "
            "following RunwayProvider's shape, and flip video_gen.active to "
            "'invideo' in config/providers.yaml — no other file needs to change."
        )
