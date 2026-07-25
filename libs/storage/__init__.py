"""Centralized media-asset storage, keyed by project id.

Every media artifact an agent produces — audio, storyboard images,
rendered video, thumbnails, captions — is written here under a path
scheme rooted at the project's own id (see local_backend.py). The string
`save_bytes` returns is exactly what belongs in `Asset.storage_path`
(libs/models/asset.py) — the database only ever stores where to find a
file, never the file itself.

Only a `local` backend exists today (bytes on this VPS's disk, under
STORAGE_ROOT). A future S3/MinIO-backed backend is a drop-in addition
behind the same `StorageBackend` interface (base.py) — callers only ever
go through `get_storage_backend()` (registry.py), never a concrete
backend class, so switching is a config change (STORAGE_BACKEND=s3), not
a rewrite of every agent that writes media — the same provider-swap
pattern used for AI providers (libs/providers).

Usage from an agent module:

    from libs.storage import get_storage_backend

    storage = get_storage_backend()
    path = storage.save_bytes(context.project_id, "voiceover", "segment_02.mp3", audio_bytes)
    # path is what gets written to Asset.storage_path
"""

from .base import StorageBackend
from .registry import get_storage_backend

__all__ = ["StorageBackend", "get_storage_backend"]
