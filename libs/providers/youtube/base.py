"""YouTube publishing provider interface.

There is only one real YouTube, so this isn't "swappable" in the sense
video/TTS/image-gen providers are — but it stays behind the same
config-driven interface so the Publisher Agent (services/agent_publisher)
can point at a stub/test double in local dev without touching the real
Data API and its quota, exactly like every other capability here, and so
a future alternative publishing backend (e.g. a different upload
mechanism, or a second platform entirely) slots in the same way any
other provider does. `YouTubeDataAPIProvider`
(youtube_data_api_provider.py) is the real, working implementation
against the YouTube Data API v3; stub_provider.py is the honest stub.

Four operations, not one `upload()` call, because publishing a video is
genuinely more than "send bytes": setting the thumbnail and adding to a
playlist are separate API calls a caller may need to retry independently
of the upload itself, and `verify_upload` exists specifically so a caller
never has to trust `upload_video`'s own return value as the last word —
see docs/architecture/03-agent-responsibilities.md §3.9 for why the
Publisher Agent always calls it before marking a project published.
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from libs.providers.base import Provider


class YouTubeProviderError(RuntimeError):
    """Base for every YouTube provider failure. A caller never needs to
    catch a vendor-specific/HTTP-level exception — only one of these.
    """


class YouTubeAuthError(YouTubeProviderError):
    """OAuth token refresh failed, or credentials are missing/invalid —
    retrying the identical call won't help without fixing credentials.
    """


class YouTubeQuotaError(YouTubeProviderError):
    """The YouTube Data API's daily quota is exhausted. Distinct from
    every other failure because retrying moments later won't help — the
    caller (the Publisher Agent, then the Manager's own retry/backoff)
    should treat this as "try again on a much longer horizon", not a
    transient blip.
    """


class YouTubeUploadError(YouTubeProviderError):
    """The call reached the Data API but failed for a reason other than
    quota or auth — rejected content, an invalid request, or a transient
    network/5xx failure that exhausted this provider's own retries (see
    youtube_data_api_provider.py's docstring for its retry policy).
    """


@dataclass(frozen=True)
class VideoMetadata:
    """Everything the Publisher Agent decides about *this* upload before
    calling the provider — title/description/tags refined for this
    video, not the provider's own concern.
    """

    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    #: YouTube's numeric video category id — "22" (People & Blogs) is a
    #: broadly-applicable default; a caller passes a different one when
    #: it knows better.
    category_id: str = "22"
    #: "private" | "unlisted" | "public".
    privacy_status: str = "private"
    #: RFC3339 timestamp — when set, YouTube auto-publishes (flips to
    #: public) at this moment instead of immediately. The Data API
    #: requires `privacy_status="private"` at upload time whenever this
    #: is set (scheduling a non-private video is rejected); the concrete
    #: provider enforces that rather than silently overriding the caller's
    #: choice.
    publish_at: str | None = None
    #: Resolved by the caller (e.g. from `Channel.persona_config`) —
    #: never invented by this provider, which has no way to know which
    #: playlists genuinely exist on the channel.
    playlist_id: str | None = None


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str
    #: YouTube's own `status.uploadStatus` — "uploaded"/"processed" are
    #: healthy; "failed"/"rejected" are real problems a caller must not
    #: treat as success.
    upload_status: str
    privacy_status: str
    #: Self-reported by the API's own response (never assumed from
    #: configuration) — the same "report back what actually happened,
    #: don't guess" convention as `libs.providers.tts.base.SynthesisResult`.
    channel_id: str
    #: When the video actually became (or is scheduled to become) public
    #: — `None` for a private/unlisted video with no `publish_at` set.
    published_at: str | None
    #: This provider's own wall-clock timestamp for when the upload call
    #: completed — distinct from `published_at`, which is YouTube's own
    #: (possibly future, possibly absent) visibility-change time.
    uploaded_at: datetime


class YouTubeProvider(Provider):
    @abstractmethod
    def upload_video(
        self, video_bytes: bytes, metadata: VideoMetadata, *, dry_run: bool | None = None
    ) -> UploadResult:
        """Upload `video_bytes` with `metadata`. `dry_run`, when `True`,
        must perform no real API call and return a clearly-synthetic
        result instead — `None` defers to this provider's own configured
        default. Must raise a `YouTubeProviderError` subclass on any
        failure, never a bare/vendor-specific exception.
        """

    @abstractmethod
    def set_thumbnail(self, video_id: str, thumbnail_bytes: bytes, *, dry_run: bool | None = None) -> None:
        """Set the thumbnail for an already-uploaded video."""

    @abstractmethod
    def add_to_playlist(self, video_id: str, playlist_id: str, *, dry_run: bool | None = None) -> None:
        """Add an already-uploaded video to a playlist."""

    @abstractmethod
    def verify_upload(self, video_id: str, *, dry_run: bool | None = None) -> UploadResult:
        """Re-fetch the video's current state from YouTube — the
        caller's own confirmation that the video genuinely exists and
        uploaded successfully, rather than trusting `upload_video`'s
        return value as the last word before marking a project published.
        `dry_run` follows the same contract as every other method here:
        `True` performs no real API call and returns a synthetic healthy
        result instead, since the Publisher Agent calls this
        unconditionally before marking a project published, dry runs
        included.
        """
