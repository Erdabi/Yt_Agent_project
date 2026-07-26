"""Google Veo video generation provider — an honest stub.

Google's Veo models are served through Vertex AI's asynchronous
long-running-operation pattern (`predictLongRunning` to start a
generation, `fetchPredictOperation` to poll it — the same mechanism
Vertex AI uses for other generative models, not something specific to
Veo), which needs OAuth2 credentials for a GCP service account — not a
static bearer API key like every other provider in this package — plus
a GCP project id and region. Wiring that up correctly (service-account
auth, exact request/response field names, GCS output handling) needs
real GCP project access this codebase doesn't have to verify against, so
this stays an honest stub rather than a guessed implementation that
would look real but silently be wrong — the explicit allowance this
capability was given ("Google Veo, or a stub if public API access is
unavailable").

Wire up a real implementation here once real GCP credentials/project
access exist: authentication (`_headers`/OAuth token acquisition),
request creation (`_create_task`, calling `predictLongRunning`), polling
(`_poll_until_complete`, calling `fetchPredictOperation`), downloading
(`_download`, reading the resulting GCS object), and Vertex-AI-specific
error handling, following the exact same shape as
`libs/providers/video_gen/runway_provider.py`'s `RunwayProvider` — the
method names below are placeholders for that future work, not a real
implementation.
"""

from typing import Any

from .base import VideoGenProvider


class VeoAPIError(RuntimeError):
    """Raised by a real Veo implementation once one exists — kept as a
    named type now so callers that might someday catch it specifically
    don't need to change when this stub becomes real.
    """


class VeoProvider(VideoGenProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        self._api_key = api_key
        self._project_id = config.get("project_id")
        self._location = config.get("location", "us-central1")
        self._model = config.get("model", "veo-3.0-generate-001")
        self._poll_interval_sec = float(config.get("poll_interval_sec", 10))
        self._poll_timeout_sec = float(config.get("poll_timeout_sec", 600))

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "Google Veo requires Vertex AI OAuth service-account credentials "
            "and a real GCP project, neither available to verify an "
            "implementation against here (see this module's docstring). Once "
            "real GCP access exists: implement OAuth token acquisition, "
            "predictLongRunning/fetchPredictOperation calls, and GCS download "
            "in _headers/_create_task/_poll_until_complete/_download below "
            "following RunwayProvider's shape, and flip video_gen.active to "
            "'veo' in config/providers.yaml — no other file needs to change."
        )
