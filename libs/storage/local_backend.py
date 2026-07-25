"""Local-filesystem storage backend.

Lays every asset out under one root directory, keyed by project id:

    {root}/{project_id}/{category}/{filename}

e.g. `data/3fe1.../voiceover/segment_02.mp3`. This is the only backend
that exists today — everything on a single Hetzner VPS, per the current
deployment target (docs/architecture/01-system-architecture.md §1.6) — but
nothing outside this file knows that; callers go through
`get_storage_backend()` (registry.py), so adding an S3/MinIO-backed
backend later never touches agent code.

`project_id`, `category`, and `filename` are path segments supplied by
application code (job payloads, agent output), not raw user input off the
wire — but they're validated anyway, defensively, since a bug or a
malformed job payload constructing e.g. `category="../../etc"` should
fail loudly here rather than silently write outside the storage root.
"""

import re
from pathlib import Path

from .base import StorageBackend

# Deliberately conservative: letters, digits, underscore, dot, hyphen. No
# "/" or "\\" (rules out any segment smuggling extra path components), and
# "." / ".." are rejected explicitly below since they'd otherwise match
# this pattern.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def save_bytes(self, project_id: str, category: str, filename: str, data: bytes) -> str:
        path = self._resolve_new(project_id, category, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self._root))

    def read_bytes(self, storage_path: str) -> bytes:
        return self._resolve_existing(storage_path).read_bytes()

    def exists(self, storage_path: str) -> bool:
        try:
            return self._resolve_existing(storage_path).exists()
        except ValueError:
            return False

    def delete(self, storage_path: str) -> None:
        path = self._resolve_existing(storage_path)
        path.unlink(missing_ok=True)

    def _resolve_new(self, project_id: str, category: str, filename: str) -> Path:
        for label, value in (
            ("project_id", project_id),
            ("category", category),
            ("filename", filename),
        ):
            self._validate_segment(label, value)
        return self._within_root(self._root / project_id / category / filename)

    def _resolve_existing(self, storage_path: str) -> Path:
        if not storage_path:
            raise ValueError("storage_path must not be empty")
        return self._within_root(self._root / storage_path)

    @staticmethod
    def _validate_segment(label: str, value: str) -> None:
        if not value or value in (".", "..") or not _SAFE_SEGMENT.match(value):
            raise ValueError(f"unsafe {label!r} for a storage path: {value!r}")

    def _within_root(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise ValueError(f"path escapes storage root: {resolved}")
        return resolved
