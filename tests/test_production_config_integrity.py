"""The committed production configuration must stay production-grade.

`config/providers.yaml` is the shared, committed configuration and the
default `PROVIDERS_CONFIG_PATH` points at it, so it is what any machine
uses unless it deliberately opts out. A machine without a GPU is expected
to opt out — copy it to `config/providers.local*.yaml` (gitignored), drop
the resolution, point `PROVIDERS_CONFIG_PATH` there — and that mechanism
is the whole reason the committed file must never absorb those smaller
numbers itself.

The failure mode is quiet and expensive: someone shrinks the committed
resolution to make a CPU box finish, it gets committed, and every later
run on real hardware silently produces low-resolution output that still
looks "correct" everywhere except in the finished video. Nothing else in
the suite would notice, because every unit test passes either way.

These assertions deliberately read the file from disk rather than through
`get_settings()`, so a local override in someone's `.env` cannot make
them pass or fail.
"""

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMMITTED_CONFIG = _REPO_ROOT / "config" / "providers.yaml"

#: The base pass must stay near what an SD1.5-class checkpoint is trained
#: for. This floor is not a quality target — it is the point below which
#: renders were measured to come back incoherent (smeared subjects,
#: repeated horizons), so it is the line between "slower" and "broken".
_MIN_BASE_WIDTH = 640
_MIN_BASE_HEIGHT = 360
#: What the hires pass must reach before the compositor's final scale to
#: 1080p. Below 1280x720 that final step is an upscale, which cannot add
#: back detail that was never rendered.
_MIN_HIRES_WIDTH = 1280
_MIN_HIRES_HEIGHT = 720


@pytest.fixture(scope="module")
def image_gen_config() -> dict:
    data = yaml.safe_load(_COMMITTED_CONFIG.read_text(encoding="utf-8"))
    return data["image_gen"]["providers"]["comfyui"]["config"]


def test_committed_config_is_the_default_path():
    """If the default ever pointed at a local override, the committed
    file would stop being what a fresh checkout actually runs.
    """
    from libs.core.config import Settings

    assert Settings.model_fields["providers_config_path"].default == "config/providers.yaml"


def test_base_render_resolution_is_production_grade(image_gen_config):
    width, height = int(image_gen_config["width"]), int(image_gen_config["height"])
    assert width >= _MIN_BASE_WIDTH, f"base width {width} is a CPU/test resolution"
    assert height >= _MIN_BASE_HEIGHT, f"base height {height} is a CPU/test resolution"


def test_the_hires_pass_reaches_at_least_720p(image_gen_config):
    """The two-pass design only pays off if the second pass actually
    lands somewhere the compositor is not forced to upscale from.
    """
    factor = float(image_gen_config["upscale_factor"])
    assert factor > 1.0, "upscale_factor <= 1 disables the hires pass entirely"
    assert int(image_gen_config["width"]) * factor >= _MIN_HIRES_WIDTH
    assert int(image_gen_config["height"]) * factor >= _MIN_HIRES_HEIGHT


def test_render_dimensions_stay_multiples_of_eight(image_gen_config):
    """Latent space is 8x8 per pixel block; ComfyUI rejects anything
    else outright, so a bad edit here fails every render at submit time.
    """
    factor = float(image_gen_config["upscale_factor"])
    for value in (
        int(image_gen_config["width"]),
        int(image_gen_config["height"]),
        int(int(image_gen_config["width"]) * factor),
        int(int(image_gen_config["height"]) * factor),
    ):
        assert value % 8 == 0, f"{value} is not a multiple of 8"


def test_sampler_and_scheduler_are_sensible(image_gen_config):
    assert image_gen_config["sampler"] == "dpmpp_2m"
    assert image_gen_config["scheduler"] == "karras"


def test_step_counts_are_not_test_shortcuts(image_gen_config):
    """Low step counts are the other half of a CPU shortcut, and unlike
    resolution they leave the image dimensions looking correct.
    """
    assert int(image_gen_config["steps"]) >= 20, "base steps look like a CPU/test shortcut"
    assert int(image_gen_config["hires_steps"]) >= 8


def test_cfg_is_in_a_usable_range(image_gen_config):
    """Far too low washes out prompt adherence; far too high produces
    the burnt, over-contrasted look.
    """
    assert 4.0 <= float(image_gen_config["cfg"]) <= 9.0


def test_hires_denoise_refines_rather_than_replaces(image_gen_config):
    """At ~1.0 the second pass ignores the base render and re-invents the
    image, discarding the composition the first pass established.
    """
    assert 0.2 <= float(image_gen_config["hires_denoise"]) <= 0.7


def test_the_committed_config_uses_real_providers_not_stubs():
    data = yaml.safe_load(_COMMITTED_CONFIG.read_text(encoding="utf-8"))
    assert data["image_gen"]["active"] != "stub"
    assert data["editor"]["active"] != "stub"
    assert data["llm"]["active"] != "stub"
    assert data["tts"]["active"] != "stub"


def test_machine_specific_overrides_stay_out_of_git():
    """The override mechanism only works if the overrides are actually
    ignored — a committed `providers.local*.yaml` would ship one
    machine's CPU settings to everyone.
    """
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config/providers.local*.yaml" in gitignore

    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "config/"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert not [path for path in tracked if "providers.local" in path]


def test_render_profile_targets_1080p_at_a_normal_frame_rate():
    """The compositor scales every visual to the profile's resolution,
    so a shrunken profile would undo the image config above.
    """
    from libs.providers.editor.profiles import DEFAULT_PROFILE_NAME, get_render_profile

    profile = get_render_profile(DEFAULT_PROFILE_NAME)
    assert profile.resolution == "1920x1080"
    assert profile.fps >= 24


def test_visual_pacing_lands_in_the_youtube_band():
    """Pacing is configuration, and the whole point of moving it there
    was to hit roughly a visual every 2-5 seconds. A profile edit that
    drifts outside that band brings back the slideshow feel.
    """
    from libs.providers.editor.profiles import DEFAULT_PROFILE_NAME, get_render_profile

    profile = get_render_profile(DEFAULT_PROFILE_NAME)
    for pacing in ("fast", "medium", "slow"):
        target = profile.seconds_per_visual(pacing)
        assert 2.0 <= target <= 5.0, f"{pacing} target {target}s is outside the 2-5s band"
    assert profile.seconds_per_visual("fast") < profile.seconds_per_visual("slow")
    assert profile.ken_burns_zoom_per_sec > 0, "Ken Burns motion disabled in the profile"
    assert profile.visual_crossfade_sec > 0, "visual crossfades disabled in the profile"
