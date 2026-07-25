"""The interface every storage backend implements.

Kept deliberately small: an agent (or the Video Agent's modules) only
ever needs to write bytes for a project and get back a path to persist
onto an `Asset.storage_path` row, and later read those same bytes back
given that path. Anything backend-specific (bucket names, credentials,
directory layout) stays inside the concrete backend, behind this
interface — see registry.py for how a caller picks one without knowing
which concrete class it is.
"""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    @abstractmethod
    def save_bytes(self, project_id: str, category: str, filename: str, data: bytes) -> str:
        """Write `data` for `project_id` under `category` (e.g. "audio",
        "storyboard", "thumbnail") as `filename`, and return the storage
        path/key to persist verbatim in `Asset.storage_path`
        (libs/models/asset.py). Callers must not construct or assume
        anything about the shape of the returned string beyond "pass it
        back to `read_bytes` unchanged" — that shape is backend-specific.
        """

    @abstractmethod
    def read_bytes(self, storage_path: str) -> bytes:
        """Read back exactly what a prior `save_bytes` call wrote, given
        the path/key it returned.
        """

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        """Whether `storage_path` currently points at real data."""

    @abstractmethod
    def delete(self, storage_path: str) -> None:
        """Remove the data at `storage_path`. A no-op if it's already gone."""
