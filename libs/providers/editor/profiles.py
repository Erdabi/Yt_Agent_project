"""Named, reusable render output-format configurations.

Resolution, frame rate, loudness target, crossfade duration, and
subtitle sizing all travel together as one bundle rather than loose
parameters passed around individually — the same "config, not code"
philosophy `config/providers.yaml` already applies to *which provider*
backs a capability, applied here to *which output format* a render
targets. A future format (a 9:16 Shorts cut, a square 1:1 cut) is a new
named entry in config/render_profiles.yaml, not a rewrite of
`FFmpegEditorProvider`.

Usage:

    from libs.providers.editor.profiles import get_render_profile

    profile = get_render_profile("long_form_1080p")
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from libs.core.config import get_settings

# repo root: libs/providers/editor/profiles.py -> editor -> providers -> libs -> root
_REPO_ROOT = Path(__file__).resolve().parents[3]


class RenderProfileError(RuntimeError):
    """The render profiles config file is missing, malformed, or names a
    profile that doesn't exist.
    """


@dataclass(frozen=True)
class RenderProfile:
    name: str
    resolution: str
    fps: int
    #: EBU R128 integrated loudness target, in LUFS, for the final
    #: audio-normalization pass — YouTube's own long-form target is
    #: around -14 LUFS; a Shorts-style profile might target the same or
    #: a platform-specific value.
    loudness_target_lufs: float
    crossfade_sec: float
    subtitle_font_size: int


@lru_cache
def _load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = _REPO_ROOT / path
    if not config_path.is_file():
        raise RenderProfileError(f"render profiles config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RenderProfileError(f"render profiles config file must be a mapping: {config_path}")
    return data


def get_render_profile(name: str) -> RenderProfile:
    settings = get_settings()
    config = _load_config(settings.render_profiles_path)
    entry = config.get(name)
    if entry is None:
        raise RenderProfileError(f"unknown render profile {name!r} (available: {sorted(config)})")
    try:
        return RenderProfile(
            name=name,
            resolution=entry["resolution"],
            fps=int(entry.get("fps", 30)),
            loudness_target_lufs=float(entry.get("loudness_target_lufs", -14.0)),
            crossfade_sec=float(entry.get("crossfade_sec", 0.5)),
            subtitle_font_size=int(entry.get("subtitle_font_size", 44)),
        )
    except KeyError as exc:
        raise RenderProfileError(f"render profile {name!r} is missing required field: {exc}") from exc
