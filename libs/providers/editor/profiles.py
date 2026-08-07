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

#: The profile used when a caller doesn't name one. Defined here rather
#: than in any one consumer because two stages now read a profile — the
#: compositor (resolution, fps, fades) and Visual Beat Planning (cutting
#: rhythm) — and they must resolve the *same* profile or a video would be
#: cut for one format and rendered for another.
DEFAULT_PROFILE_NAME = "long_form_1080p"


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
    #: Fade duration applied between whole *segments*.
    crossfade_sec: float
    subtitle_font_size: int
    #: Fractional scale added per second by the slow Ken Burns push on
    #: still images (0.015 = 1.5% per second). `0` disables the effect,
    #: which is what a profile for a source that already moves (all-video
    #: b-roll) would set.
    ken_burns_zoom_per_sec: float = 0.015
    #: Dissolve duration between consecutive visuals *within* one
    #: segment — distinct from `crossfade_sec` above, which is between
    #: segments. `0` makes them hard cuts.
    visual_crossfade_sec: float = 0.4
    #: Target hold time for one visual, selected by the segment's own
    #: `pacing` — see config/render_profiles.yaml for the full rationale.
    #: Read by Visual Beat Planning, not by the compositor: cutting
    #: rhythm and output format travel together, so a Shorts profile
    #: cuts faster without any code knowing what "Shorts" means.
    seconds_per_visual_fast: float = 2.5
    seconds_per_visual_medium: float = 3.5
    seconds_per_visual_slow: float = 5.0
    #: Floor on how briefly any visual may be shown; subdivision stops
    #: rather than producing beats shorter than this.
    min_visual_duration_sec: float = 2.0

    def seconds_per_visual(self, pacing: str) -> float:
        """Target hold time for a segment with this `pacing` value
        (`libs.schemas.script_production.Pacing`). An unrecognized value
        falls back to the medium target rather than raising: `pacing` is
        model-supplied, so an unexpected string is a data problem to
        absorb, not a reason to fail a render.
        """
        return {
            "fast": self.seconds_per_visual_fast,
            "medium": self.seconds_per_visual_medium,
            "slow": self.seconds_per_visual_slow,
        }.get(pacing, self.seconds_per_visual_medium)


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
            ken_burns_zoom_per_sec=float(entry.get("ken_burns_zoom_per_sec", 0.015)),
            visual_crossfade_sec=float(entry.get("visual_crossfade_sec", 0.4)),
            seconds_per_visual_fast=float(entry.get("seconds_per_visual_fast", 2.5)),
            seconds_per_visual_medium=float(entry.get("seconds_per_visual_medium", 3.5)),
            seconds_per_visual_slow=float(entry.get("seconds_per_visual_slow", 5.0)),
            min_visual_duration_sec=float(entry.get("min_visual_duration_sec", 2.0)),
        )
    except KeyError as exc:
        raise RenderProfileError(f"render profile {name!r} is missing required field: {exc}") from exc
