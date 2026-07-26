"""Runway ML video generation provider.

A real, working adapter against Runway's public developer API
(https://docs.dev.runwayml.com/), used by the Video Agent's Asset
Generation module for `ai_video`/`animation` asset requirements
(services/agent_video/app/modules/asset_generation.py). Everything
specific to this one vendor — authentication, request construction,
polling, downloading the finished clip, and Runway-specific error
handling — is encapsulated behind the same `VideoGenProvider.generate(prompt)
-> bytes` interface every other video_gen provider implements, so nothing
outside this file (Asset Generation, the Asset Cache, config/providers.yaml)
needs to know Runway exists specifically. Swapping this out for a
different provider is a config/providers.yaml change (video_gen.active),
not a code change.
"""

import time
from typing import Any

import requests

from .base import VideoGenProvider

#: Pinned per Runway's API versioning scheme
#: (https://docs.dev.runwayml.com/api-details/versioning/) — required on
#: every request so Runway knows which request/response shape to honor.
_API_VERSION = "2024-11-06"


class RunwayAPIError(RuntimeError):
    """Runway rejected a request outright or reported a task that
    terminally failed — distinct from a transient network/5xx error
    (which this provider already retries internally; see
    `_request_with_retry`). Raised honestly rather than swallowed, so the
    Video Agent job fails and the Manager's existing retry-routing
    decides what happens next, the same as every other provider failure
    in this pipeline.
    """


class RunwayProvider(VideoGenProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        if not api_key:
            raise RunwayAPIError(
                "RunwayProvider requires an API key — set the environment "
                "variable named by config/providers.yaml's video_gen.runway."
                "secret_ref (e.g. RUNWAY_API_KEY) before selecting 'runway' "
                "as the active video_gen provider."
            )
        self._api_key = api_key
        self._base_url = config.get("base_url", "https://api.dev.runwayml.com")
        self._model = config.get("model", "gen4_turbo")
        self._ratio = config.get("ratio", "1280:720")
        self._duration = config.get("duration", 5)
        self._poll_interval_sec = float(config.get("poll_interval_sec", 5))
        self._poll_timeout_sec = float(config.get("poll_timeout_sec", 300))
        self._max_attempts = int(config.get("max_attempts", 3))
        self._retry_backoff_sec = float(config.get("retry_backoff_sec", 2))

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        task_id = self._create_task(prompt, **kwargs)
        output_url = self._poll_until_complete(task_id)
        return self._download(output_url)

    # --- authentication ---------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Runway-Version": _API_VERSION,
        }

    # --- request creation ---------------------------------------------------

    def _create_task(self, prompt: str, **kwargs: Any) -> str:
        body = {
            "model": kwargs.get("model", self._model),
            "promptText": prompt,
            "ratio": kwargs.get("ratio", self._ratio),
            "duration": kwargs.get("duration", self._duration),
        }
        response = self._request_with_retry("POST", f"{self._base_url}/v1/text_to_video", json=body)
        data = self._json(response)
        task_id = data.get("id")
        if not task_id:
            raise RunwayAPIError(f"Runway task creation returned no task id: {data!r}")
        return task_id

    # --- polling ---------------------------------------------------------

    def _poll_until_complete(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout_sec
        while True:
            response = self._request_with_retry("GET", f"{self._base_url}/v1/tasks/{task_id}")
            data = self._json(response)
            status = data.get("status")

            if status == "SUCCEEDED":
                output = data.get("output") or []
                if not output:
                    raise RunwayAPIError(f"Runway task {task_id} succeeded with no output: {data!r}")
                return output[0]
            if status == "FAILED":
                reason = data.get("failure") or data.get("error") or data.get("failureCode") or "unknown reason"
                raise RunwayAPIError(f"Runway task {task_id} failed: {reason}")
            if status not in ("PENDING", "RUNNING", "THROTTLED"):
                raise RunwayAPIError(f"Runway task {task_id} returned an unrecognized status: {status!r}")

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Runway task {task_id} did not finish within {self._poll_timeout_sec:.0f}s "
                    f"(last status: {status!r})"
                )
            time.sleep(self._poll_interval_sec)

    # --- downloading -------------------------------------------------------

    def _download(self, url: str) -> bytes:
        # The output URL is a pre-signed asset link, not a Runway API
        # endpoint — no auth headers belong on this request.
        response = self._request_with_retry("GET", url, authenticated=False)
        return response.content

    # --- provider-specific error handling / retry --------------------------

    def _request_with_retry(
        self, method: str, url: str, *, json: dict | None = None, authenticated: bool = True
    ) -> requests.Response:
        headers = self._headers() if authenticated else None
        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            try:
                response = requests.request(method, url, headers=headers, json=json, timeout=30)
            except requests.exceptions.RequestException as exc:
                if is_last_attempt:
                    raise RunwayAPIError(
                        f"Runway {method} {url} failed after {attempt} attempts: {exc}"
                    ) from exc
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 500:
                # Transient server-side failure — worth retrying.
                if is_last_attempt:
                    raise RunwayAPIError(
                        f"Runway {method} {url} returned {response.status_code} after "
                        f"{attempt} attempts: {response.text[:500]}"
                    )
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 400:
                # A 4xx (bad auth, bad request, rate limit) won't be fixed
                # by retrying the identical request — fail immediately
                # rather than burning attempts.
                raise RunwayAPIError(
                    f"Runway {method} {url} returned {response.status_code}: {response.text[:500]}"
                )

            return response

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise RunwayAPIError(
                f"Runway returned a non-JSON response ({response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
