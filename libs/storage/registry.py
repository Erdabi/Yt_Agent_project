"""Picks the active storage backend from settings, cached per process —
the same pattern `libs.core.db` uses for engines. Every caller goes
through `get_storage_backend()`, never a concrete backend class, so
switching STORAGE_BACKEND is a config change, not a code change.
"""

from functools import lru_cache

from libs.core.config import get_settings

from .base import StorageBackend
from .local_backend import LocalStorageBackend


@lru_cache
def get_storage_backend() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorageBackend(settings.storage_root)
    raise ValueError(f"unknown STORAGE_BACKEND: {settings.storage_backend!r}")
