"""The default `youtube` provider (see config/providers.yaml's
`active: stub`) — stays the default so nothing publishes to a real
YouTube channel without real OAuth credentials configured. A real
implementation exists alongside this one (youtube_data_api_provider.py);
flip `youtube.active` to `youtube_data_api` (or set
`YOUTUBE_PROVIDER=youtube_data_api`) once real OAuth credentials are set.
"""

from .base import UploadResult, VideoMetadata, YouTubeProvider


class StubYouTubeProvider(YouTubeProvider):
    def upload_video(
        self, video_bytes: bytes, metadata: VideoMetadata, *, dry_run: bool | None = None
    ) -> UploadResult:
        raise NotImplementedError(
            "No real YouTube publishing provider is configured. Add one under "
            "libs/providers/youtube/, register it in config/providers.yaml, and "
            "set youtube.active to its name."
        )

    def set_thumbnail(self, video_id: str, thumbnail_bytes: bytes, *, dry_run: bool | None = None) -> None:
        raise NotImplementedError(
            "No real YouTube publishing provider is configured. Add one under "
            "libs/providers/youtube/, register it in config/providers.yaml, and "
            "set youtube.active to its name."
        )

    def add_to_playlist(self, video_id: str, playlist_id: str, *, dry_run: bool | None = None) -> None:
        raise NotImplementedError(
            "No real YouTube publishing provider is configured. Add one under "
            "libs/providers/youtube/, register it in config/providers.yaml, and "
            "set youtube.active to its name."
        )

    def verify_upload(self, video_id: str, *, dry_run: bool | None = None) -> UploadResult:
        raise NotImplementedError(
            "No real YouTube publishing provider is configured. Add one under "
            "libs/providers/youtube/, register it in config/providers.yaml, and "
            "set youtube.active to its name."
        )
