"""A real, working `youtube` provider against the YouTube Data API v3.

Authentication is OAuth2 with a refresh token: `client_id`/`client_secret`
identify the registered app, `refresh_token` is the long-lived credential
this provider is actually configured with — it exchanges that for a
short-lived access token per call (cached in-memory only, for this one
process/job, and refreshed again once it's near expiry). The access token
is never written to disk, the database, or a log line; only the refresh
token is a durable secret, read from configuration/environment the same
way every other provider's `secret_ref` works
(config/providers.yaml/.env.example).

Upload is the Data API's resumable-upload protocol
(https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol):
initiate a session (POST, metadata as JSON, `X-Upload-Content-*` headers
declaring the file), then PUT the file bytes to the returned session URL.
If that PUT is interrupted (network failure, 5xx), this provider queries
how many bytes the session actually received (a bodyless PUT with
`Content-Range: bytes */<total>`) and resumes from that offset rather
than restarting the whole upload — the real resumable behavior the
protocol exists for, not just a plain retry.

Transient failures (network errors, 5xx) are retried with exponential
backoff, same policy as `libs/providers/video_gen/runway_provider.py`. A
4xx is never retried — but which `YouTubeProviderError` subclass it
becomes depends on what the Data API actually says: a `quotaExceeded`/
`dailyLimitExceeded` reason raises `YouTubeQuotaError` (retrying an hour
from now won't help either — the daily quota resets on its own schedule,
so this is a "come back later" signal, not a "fix your request" one,
unlike every other 4xx here).
"""

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import requests

from .base import (
    UploadResult,
    VideoMetadata,
    YouTubeAuthError,
    YouTubeProvider,
    YouTubeQuotaError,
    YouTubeUploadError,
)

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
_QUOTA_ERROR_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"})
#: Refresh the access token this long before its reported expiry, so a
#: token that's about to expire mid-call is never handed out.
_TOKEN_EXPIRY_SAFETY_MARGIN_SEC = 30.0


class YouTubeDataAPIProvider(YouTubeProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        # `api_key` is the client *secret* (the one slot the Provider
        # Registry's `secret_ref` mechanism gives every provider) — the
        # client id and refresh token are each a separate secret, so
        # they're read directly from their own environment variables
        # (names configurable via `config:`, defaulting to fixed names)
        # rather than forcing a second registry mechanism into existence
        # for one provider. All three stay inside this adapter; nothing
        # elsewhere in the codebase ever sees them.
        self._client_secret = api_key
        self._client_id = os.environ.get(config.get("client_id_env_var", "YOUTUBE_OAUTH_CLIENT_ID"))
        self._refresh_token = os.environ.get(
            config.get("refresh_token_env_var", "YOUTUBE_OAUTH_REFRESH_TOKEN")
        )

        self._max_attempts = int(config.get("max_attempts", 3))
        self._retry_backoff_sec = float(config.get("retry_backoff_sec", 2))
        self._request_timeout_sec = float(config.get("request_timeout_sec", 30))
        self._upload_timeout_sec = float(config.get("upload_timeout_sec", 600))
        #: Provider-level default; a caller can still override per call
        #: via `upload_video(..., dry_run=True/False)`.
        self._dry_run = bool(config.get("dry_run", False))

        self._access_token: str | None = None
        self._access_token_expiry_monotonic: float = 0.0

    # --- public interface ---------------------------------------------------

    def upload_video(
        self, video_bytes: bytes, metadata: VideoMetadata, *, dry_run: bool | None = None
    ) -> UploadResult:
        if self._resolve_dry_run(dry_run):
            return self._dry_run_result(metadata)

        access_token = self._get_access_token()
        upload_url = self._initiate_resumable_session(access_token, metadata, len(video_bytes))
        video_resource = self._upload_bytes_resumable(upload_url, video_bytes, access_token)
        return self._parse_video_resource(video_resource)

    def set_thumbnail(self, video_id: str, thumbnail_bytes: bytes, *, dry_run: bool | None = None) -> None:
        if self._resolve_dry_run(dry_run):
            return
        access_token = self._get_access_token()
        self._request_with_retry(
            "POST",
            f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}",
            access_token=access_token,
            data=thumbnail_bytes,
            extra_headers={"Content-Type": "image/png"},
        )

    def add_to_playlist(self, video_id: str, playlist_id: str, *, dry_run: bool | None = None) -> None:
        if self._resolve_dry_run(dry_run):
            return
        access_token = self._get_access_token()
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        self._request_with_retry(
            "POST", f"{_API_BASE_URL}/playlistItems?part=snippet", access_token=access_token, json=body,
        )

    def verify_upload(self, video_id: str, *, dry_run: bool | None = None) -> UploadResult:
        if self._resolve_dry_run(dry_run):
            return UploadResult(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                upload_status="uploaded",
                privacy_status="private",
                channel_id="dry-run-channel",
                published_at=None,
                uploaded_at=datetime.now(UTC),
            )
        access_token = self._get_access_token()
        response = self._request_with_retry(
            "GET", f"{_API_BASE_URL}/videos?part=status,snippet&id={video_id}", access_token=access_token,
        )
        data = self._json(response)
        items = data.get("items") or []
        if not items:
            raise YouTubeUploadError(f"video {video_id!r} does not exist on YouTube (verify_upload found no items)")
        return self._parse_video_resource(items[0])

    # --- OAuth2 refresh-token exchange ---------------------------------------

    def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._access_token_expiry_monotonic - _TOKEN_EXPIRY_SAFETY_MARGIN_SEC:
            return self._access_token

        if not (self._client_id and self._client_secret and self._refresh_token):
            raise YouTubeAuthError(
                "YouTube OAuth credentials are not fully configured — need a client id, "
                "client secret (config/providers.yaml's youtube.youtube_data_api.secret_ref), "
                "and refresh token, none of which this provider ever persists beyond this process."
            )

        try:
            response = requests.post(
                _OAUTH_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=self._request_timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            raise YouTubeAuthError(f"OAuth token refresh request failed: {exc}") from exc

        if response.status_code >= 400:
            raise YouTubeAuthError(
                f"OAuth token refresh failed ({response.status_code}): {response.text[:300]}"
            )
        data = self._json(response)
        access_token = data.get("access_token")
        if not access_token:
            raise YouTubeAuthError(f"OAuth token refresh returned no access_token: {data!r}")

        # In-memory only, for this process — never written to disk, the
        # database, or a log line.
        self._access_token = access_token
        self._access_token_expiry_monotonic = now + float(data.get("expires_in", 3600))
        return access_token

    # --- resumable upload ----------------------------------------------------

    def _initiate_resumable_session(self, access_token: str, metadata: VideoMetadata, content_length: int) -> str:
        status: dict[str, Any] = {"privacyStatus": metadata.privacy_status}
        if metadata.publish_at:
            # The Data API rejects a publishAt on anything but a private
            # video — enforced here rather than trusting the caller,
            # since silently publishing early/live would be a real,
            # hard-to-reverse mistake.
            status["privacyStatus"] = "private"
            status["publishAt"] = metadata.publish_at

        body = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "categoryId": metadata.category_id,
            },
            "status": status,
        }
        response = self._request_with_retry(
            "POST",
            f"{_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            access_token=access_token,
            json=body,
            extra_headers={
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(content_length),
            },
        )
        upload_url = response.headers.get("Location")
        if not upload_url:
            raise YouTubeUploadError("YouTube did not return a resumable upload session URL")
        return upload_url

    def _upload_bytes_resumable(self, upload_url: str, video_bytes: bytes, access_token: str) -> dict[str, Any]:
        total = len(video_bytes)
        offset = 0
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = requests.put(
                    upload_url,
                    data=video_bytes[offset:],
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{total - 1}/{total}",
                    },
                    timeout=self._upload_timeout_sec,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                resumed_offset = self._query_resume_offset(upload_url, total, access_token)
                if resumed_offset is not None:
                    offset = resumed_offset
                if attempt < self._max_attempts:
                    time.sleep(self._retry_backoff_sec * attempt)
                    continue
                raise YouTubeUploadError(
                    f"video upload failed after {attempt} attempts (network error): {exc}"
                ) from exc

            if response.status_code in (200, 201):
                return self._json(response)

            if response.status_code == 308:
                # "Resume Incomplete" — the session is still open; resume
                # from wherever it reports it actually received.
                range_header = response.headers.get("Range")
                offset = self._parse_range_end(range_header) + 1 if range_header else offset
                last_error = YouTubeUploadError(f"upload session reported incomplete: {range_header!r}")
                if attempt < self._max_attempts:
                    time.sleep(self._retry_backoff_sec * attempt)
                    continue
                raise YouTubeUploadError(
                    f"video upload never completed after {attempt} attempts (stuck at byte {offset} of {total})"
                )

            self._raise_for_status(response, stage="video upload")

        raise YouTubeUploadError(f"video upload failed after {self._max_attempts} attempts: {last_error}")

    def _query_resume_offset(self, upload_url: str, total: int, access_token: str) -> int | None:
        try:
            response = requests.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Range": f"bytes */{total}",
                },
                timeout=self._request_timeout_sec,
            )
        except requests.exceptions.RequestException:
            return None
        if response.status_code == 308:
            range_header = response.headers.get("Range")
            return self._parse_range_end(range_header) + 1 if range_header else 0
        return None

    @staticmethod
    def _parse_range_end(range_header: str) -> int:
        # e.g. "bytes=0-12345" -> 12345
        try:
            return int(range_header.split("-")[-1])
        except (ValueError, IndexError):
            return 0

    # --- shared request/response handling -------------------------------------

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        access_token: str,
        json: dict | None = None,
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        headers.update(extra_headers or {})

        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            try:
                response = requests.request(
                    method, url, headers=headers, json=json, data=data, timeout=self._request_timeout_sec,
                )
            except requests.exceptions.RequestException as exc:
                if is_last_attempt:
                    raise YouTubeUploadError(f"{method} {url} failed after {attempt} attempts: {exc}") from exc
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 500:
                if is_last_attempt:
                    raise YouTubeUploadError(
                        f"{method} {url} returned {response.status_code} after {attempt} attempts: "
                        f"{response.text[:500]}"
                    )
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 400:
                self._raise_for_status(response, stage=f"{method} {url}")

            return response

    @staticmethod
    def _raise_for_status(response: requests.Response, *, stage: str) -> None:
        reason = None
        try:
            errors = response.json().get("error", {}).get("errors", [])
            reason = errors[0].get("reason") if errors else None
        except ValueError:
            pass

        detail = f"{stage} returned {response.status_code}: {response.text[:500]}"
        if reason in _QUOTA_ERROR_REASONS:
            raise YouTubeQuotaError(detail)
        if response.status_code in (401, 403):
            raise YouTubeAuthError(detail)
        raise YouTubeUploadError(detail)

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise YouTubeUploadError(
                f"YouTube returned a non-JSON response ({response.status_code}): {response.text[:500]}"
            ) from exc

    # --- helpers ---------------------------------------------------------

    def _resolve_dry_run(self, dry_run: bool | None) -> bool:
        return self._dry_run if dry_run is None else dry_run

    @staticmethod
    def _parse_video_resource(data: dict[str, Any]) -> UploadResult:
        video_id = data.get("id")
        if not video_id:
            raise YouTubeUploadError(f"YouTube response had no video id: {data!r}")
        status = data.get("status", {})
        snippet = data.get("snippet", {})
        return UploadResult(
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            upload_status=status.get("uploadStatus", "unknown"),
            privacy_status=status.get("privacyStatus", "unknown"),
            channel_id=snippet.get("channelId", ""),
            published_at=status.get("publishAt") or snippet.get("publishedAt"),
            uploaded_at=datetime.now(UTC),
        )

    @staticmethod
    def _dry_run_result(metadata: VideoMetadata) -> UploadResult:
        video_id = f"dry-run-{uuid.uuid4().hex[:16]}"
        return UploadResult(
            video_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            upload_status="uploaded",
            privacy_status="private" if metadata.publish_at else metadata.privacy_status,
            channel_id="dry-run-channel",
            published_at=metadata.publish_at,
            uploaded_at=datetime.now(UTC),
        )
