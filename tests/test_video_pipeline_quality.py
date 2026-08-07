"""Regression tests for the properties that make a generated video
watchable rather than a looping slideshow.

Every assertion here traces back to a defect observed in a real
end-to-end run: a 308-second video built from 5 unique images and 8
unique narration clips, one image held on screen for 12 of its 21
segments, each visual static for 15-19 seconds at a time. The
root-cause chain was three links long — a script whose beats repeated,
a content-addressed asset cache that collapsed those duplicates onto
identical files, and one static visual per segment — so it is checked
here at all three levels rather than only at the end.

Nothing here needs Postgres, Redis, or any provider: Visual Beat
Planning, Asset Planning, Timeline Building, and prompt composition are
all pure functions over in-memory data by design (see each module's
docstring), which is exactly what makes these properties cheap to assert
on every run. The one exception is the compositor test at the bottom,
which shells out to the real `ffmpeg` binary — mocking it would prove
nothing about the timing arithmetic it is there to verify.
"""

import math
import shutil
import subprocess

import pytest

from libs.providers.tts.base import WordTiming
from libs.schemas.script_production import (
    AssetRequirement,
    AssetType,
    Pacing,
    SegmentProductionMetadata,
    TransitionType,
)
from services.agent_video.app.asset_cache import AssetCacheKey
from services.agent_video.app.modules.asset_planning import AssetPlanningModule
from services.agent_video.app.modules.timeline_building import TimelineBuildingModule
from libs.providers.editor.profiles import DEFAULT_PROFILE_NAME, get_render_profile
from services.agent_video.app.modules.visual_beat_planning import VisualBeatPlanningModule
from services.agent_video.app.pipeline_schema import (
    ResolvedAsset,
    SegmentInput,
    VisualBeat,
    VoiceSegment,
)
from services.agent_video.app.visual_prompt import compose_visual_prompt

# --- builders ---------------------------------------------------------------


def _production(
    *,
    pacing: Pacing = Pacing.MEDIUM,
    requirements: list[AssetRequirement] | None = None,
    camera_framing: str = "wide establishing shot",
    narration_emotion: str = "curious",
) -> SegmentProductionMetadata:
    return SegmentProductionMetadata(
        camera_framing=camera_framing,
        asset_requirements=requirements
        or [AssetRequirement(asset_type=AssetType.AI_IMAGE, description="a busy harbour at dawn")],
        transition_type=TransitionType.FADE,
        pacing=pacing,
        narration_emotion=narration_emotion,
        emphasis_words=[],
        estimated_speech_wpm=150,
    )


def _segment(
    segment_id: str = "seg-1",
    order_index: int = 0,
    text: str | None = None,
    **production_kwargs,
) -> SegmentInput:
    return SegmentInput(
        segment_id=segment_id,
        order_index=order_index,
        segment_type="main_section",
        text=text
        or (
            "Traders carried the beans north across the desert and into the port cities. "
            "Merchants argued over prices while dockworkers hauled sacks onto waiting ships. "
            "Within a decade the trade had reshaped the whole coastline."
        ),
        scene_notes=None,
        visual_notes=None,
        production=_production(**production_kwargs),
    )


def _voice(
    segment_id: str = "seg-1",
    order_index: int = 0,
    duration_sec: float = 19.25,
    word_timings: list[WordTiming] | None = None,
) -> VoiceSegment:
    return VoiceSegment(
        segment_id=segment_id,
        order_index=order_index,
        asset_id=f"asset-{segment_id}",
        storage_path=f"path/{segment_id}.mp3",
        duration_sec=duration_sec,
        word_timings=word_timings,
        provider_name="KokoroTTSProvider",
    )


# --- visual density: enough visuals for the script --------------------------


@pytest.mark.parametrize("duration_sec", [8.0, 12.5, 19.25, 30.0, 45.0])
def test_long_segments_are_split_into_several_visuals(duration_sec: float):
    """The core defect: a 19-second segment used to resolve to exactly
    one still image, held motionless for its whole duration.
    """
    beats = VisualBeatPlanningModule().plan(
        [_segment()], [_voice(duration_sec=duration_sec)]
    )
    assert len(beats) > 1
    # Never longer than the pacing target allows.
    target = get_render_profile(DEFAULT_PROFILE_NAME).seconds_per_visual("medium")
    assert all(beat.duration_sec <= target + 0.01 for beat in beats)


def test_visual_count_scales_with_narration_length():
    plan = VisualBeatPlanningModule().plan
    short = plan([_segment()], [_voice(duration_sec=6.0)])
    long = plan([_segment()], [_voice(duration_sec=36.0)])
    assert len(long) > len(short)


def test_faster_pacing_yields_more_visuals_than_slower():
    """`pacing` is the Script Agent's own expressed intent — a beat
    marked fast should actually cut faster, not just be labelled that
    way.
    """
    plan = VisualBeatPlanningModule().plan
    fast = plan([_segment(pacing=Pacing.FAST)], [_voice(duration_sec=24.0)])
    slow = plan([_segment(pacing=Pacing.SLOW)], [_voice(duration_sec=24.0)])
    assert len(fast) > len(slow)


def test_no_visual_is_shown_too_briefly_to_register():
    """Subdividing must stop before beats become flickers — a short
    segment gets fewer visuals rather than impossibly short ones.
    """
    beats = VisualBeatPlanningModule().plan(
        [_segment(pacing=Pacing.FAST)], [_voice(duration_sec=5.0)]
    )
    floor = get_render_profile(DEFAULT_PROFILE_NAME).min_visual_duration_sec
    assert all(beat.duration_sec >= floor - 1e-9 for beat in beats)


def test_visual_pacing_is_configurable_not_hardcoded(tmp_path):
    """Cutting rhythm has to be a config change, not a code change.

    Renders the same narration under two profiles that differ *only* in
    their seconds-per-visual targets, and requires the tighter one to
    actually produce more visuals — which fails if the module ever goes
    back to reading its own constants.
    """
    from libs.core.config import get_settings
    from libs.providers.editor import profiles as profiles_module

    def _profile(name: str, target: float) -> str:
        return (
            f"{name}:\n"
            "  resolution: '1920x1080'\n"
            "  fps: 30\n"
            "  loudness_target_lufs: -14\n"
            "  crossfade_sec: 0.5\n"
            "  subtitle_font_size: 44\n"
            f"  seconds_per_visual_fast: {target}\n"
            f"  seconds_per_visual_medium: {target}\n"
            f"  seconds_per_visual_slow: {target}\n"
            "  min_visual_duration_sec: 0.5\n"
        )

    config = tmp_path / "profiles.yaml"
    config.write_text(_profile("leisurely", 6.0) + _profile("snappy", 2.0), encoding="utf-8")

    settings = get_settings()
    original = settings.render_profiles_path
    profiles_module._load_config.cache_clear()
    object.__setattr__(settings, "render_profiles_path", str(config))
    try:
        leisurely = VisualBeatPlanningModule("leisurely").plan(
            [_segment()], [_voice(duration_sec=24.0)]
        )
        snappy = VisualBeatPlanningModule("snappy").plan(
            [_segment()], [_voice(duration_sec=24.0)]
        )
    finally:
        object.__setattr__(settings, "render_profiles_path", original)
        profiles_module._load_config.cache_clear()

    assert len(snappy) > len(leisurely)
    assert len(leisurely) == 4  # 24s / 6s
    assert len(snappy) == 12  # 24s / 2s


def test_default_profile_cuts_within_the_intended_band():
    """The shipped long-form defaults should hold a visual for roughly
    2-5 seconds — the rhythm of a modern educational channel, and the
    band the profile's own comments document.
    """
    profile = get_render_profile(DEFAULT_PROFILE_NAME)
    for pacing in ("fast", "medium", "slow"):
        assert 2.0 <= profile.seconds_per_visual(pacing) <= 5.0
    assert profile.seconds_per_visual("fast") < profile.seconds_per_visual("slow")


def test_unknown_pacing_falls_back_to_medium_rather_than_raising():
    """`pacing` is model-supplied free-ish text, so an unexpected value
    must degrade rather than fail a whole render.
    """
    profile = get_render_profile(DEFAULT_PROFILE_NAME)
    assert profile.seconds_per_visual("bewildered") == profile.seconds_per_visual("medium")


def test_very_short_segment_still_gets_exactly_one_visual():
    beats = VisualBeatPlanningModule().plan([_segment()], [_voice(duration_sec=1.2)])
    assert len(beats) == 1
    assert beats[0].duration_sec == pytest.approx(1.2)


# --- beat timing: no gaps, no overlaps --------------------------------------


def test_beats_tile_their_segment_exactly_with_no_gaps_or_overlaps():
    duration = 19.25
    beats = VisualBeatPlanningModule().plan([_segment()], [_voice(duration_sec=duration)])

    assert beats[0].start_sec == pytest.approx(0.0)
    assert beats[-1].end_sec == pytest.approx(duration)
    for earlier, later in zip(beats, beats[1:]):
        assert later.start_sec == pytest.approx(earlier.end_sec), "gap or overlap between beats"
    assert sum(beat.duration_sec for beat in beats) == pytest.approx(duration)


def test_every_beat_has_a_positive_duration():
    beats = VisualBeatPlanningModule().plan([_segment()], [_voice(duration_sec=17.0)])
    assert all(beat.duration_sec > 0 for beat in beats)


def test_beat_indices_are_contiguous_and_zero_based():
    beats = VisualBeatPlanningModule().plan([_segment()], [_voice(duration_sec=19.25)])
    assert [beat.beat_index for beat in beats] == list(range(len(beats)))


# --- beat content: each visual matches what is being said over it -----------


def test_each_beat_covers_a_different_slice_of_the_narration():
    beats = VisualBeatPlanningModule().plan([_segment()], [_voice(duration_sec=19.25)])
    texts = [beat.narration_text for beat in beats]
    assert len(set(texts)) == len(texts), "beats must not all describe the same narration"


def test_real_word_timings_are_used_to_place_narration_when_available():
    """With provider-supplied timing, a word spoken late must land in a
    late beat — not merely at its proportional position in the string.
    """
    timings = [
        WordTiming(word="alpha", start_sec=0.0, end_sec=1.0),
        WordTiming(word="bravo", start_sec=1.0, end_sec=2.0),
        WordTiming(word="yankee", start_sec=9.0, end_sec=10.0),
        WordTiming(word="zulu", start_sec=10.0, end_sec=11.0),
    ]
    beats = VisualBeatPlanningModule().plan(
        [_segment(text="alpha bravo yankee zulu")],
        [_voice(duration_sec=12.0, word_timings=timings)],
    )
    assert "alpha" in beats[0].narration_text
    assert "zulu" in beats[-1].narration_text
    assert "zulu" not in beats[0].narration_text


def test_missing_narration_fails_loudly_rather_than_guessing():
    with pytest.raises(ValueError, match="no voice segment"):
        VisualBeatPlanningModule().plan([_segment()], [])


# --- asset planning: no unintended reuse ------------------------------------


def _plan_assets(segments, voices):
    beats = VisualBeatPlanningModule().plan(segments, voices)
    return beats, AssetPlanningModule().plan(segments, beats)


def test_every_visual_beat_becomes_exactly_one_planned_asset():
    beats, planned = _plan_assets([_segment()], [_voice(duration_sec=19.25)])
    visuals = [asset for asset in planned if asset.beat_index is not None]
    assert len(visuals) == len(beats)


def test_planned_visuals_never_share_a_cache_identity():
    """The reuse link in the original defect: identical prompts hit the
    content-addressed cache and collapsed onto one image file.
    """
    _, planned = _plan_assets(
        [_segment("s1", 0), _segment("s2", 1)],
        [_voice("s1", 0, 19.25), _voice("s2", 1, 19.25)],
    )
    keys = [asset.variation_key for asset in planned if asset.variation_key is not None]
    assert len(keys) == len(set(keys)), "two visuals would resolve to the same cached asset"


def test_identical_segments_still_produce_distinct_visuals():
    """Even when two segments are word-for-word identical — the exact
    shape that produced a looping video — their visuals must not be the
    same file.
    """
    text = "The harbour filled with ships before the season turned."
    _, planned = _plan_assets(
        [_segment("s1", 0, text=text), _segment("s2", 1, text=text)],
        [_voice("s1", 0, 12.0), _voice("s2", 1, 12.0)],
    )
    keys = [asset.variation_key for asset in planned if asset.variation_key is not None]
    assert len(keys) == len(set(keys))


def test_a_visual_requirement_is_not_planned_twice():
    """Visual Beat Planning expands provider-generated visuals; Asset
    Planning must not then plan the same requirement a second time as a
    segment-wide asset.
    """
    _, planned = _plan_assets([_segment()], [_voice(duration_sec=19.25)])
    assert all(asset.beat_index is not None for asset in planned)


def test_segment_wide_audio_requirements_are_planned_once_not_per_beat():
    segment = _segment(
        requirements=[
            AssetRequirement(asset_type=AssetType.AI_IMAGE, description="a harbour"),
            AssetRequirement(asset_type=AssetType.SOUND_EFFECT, description="gulls calling"),
        ]
    )
    _, planned = _plan_assets([segment], [_voice(duration_sec=19.25)])
    audio = [asset for asset in planned if asset.is_audio]
    assert len(audio) == 1, "a sound effect belongs to the segment, not to every visual beat"


def test_beats_cycle_through_all_declared_visual_requirements():
    segment = _segment(
        requirements=[
            AssetRequirement(asset_type=AssetType.AI_IMAGE, description="a harbour at dawn"),
            AssetRequirement(asset_type=AssetType.MAP, description="a trade route map"),
        ]
    )
    beats, _ = _plan_assets([segment], [_voice(duration_sec=19.25)])
    assert {beat.asset_type for beat in beats} == {AssetType.AI_IMAGE, AssetType.MAP}


# --- cache key ---------------------------------------------------------------


def test_variation_key_separates_otherwise_identical_cache_requests():
    base = {
        "capability": "image_gen",
        "provider_name": "ComfyUIImageProvider",
        "asset_type": "ai_image",
        "prompt": "a harbour at dawn",
    }
    first = AssetCacheKey(**base, variation_key="seg-1:0")
    second = AssetCacheKey(**base, variation_key="seg-1:1")
    assert first.compute_hash() != second.compute_hash()


def test_same_variation_key_still_hits_the_cache_on_a_rerun():
    """Deliberately *not* random: re-running the same job must reuse its
    own earlier work rather than regenerate every visual.
    """
    key = dict(
        capability="image_gen",
        provider_name="ComfyUIImageProvider",
        asset_type="ai_image",
        prompt="a harbour at dawn",
        variation_key="seg-1:0",
    )
    assert AssetCacheKey(**key).compute_hash() == AssetCacheKey(**key).compute_hash()


def test_keys_without_a_variation_key_hash_exactly_as_before():
    """Backwards compatibility: adding the field must not invalidate
    every already-cached asset (narration, sound effects, thumbnails).
    """
    key = AssetCacheKey(
        capability="tts",
        provider_name="KokoroTTSProvider",
        asset_type="narration",
        prompt="hello world",
    )
    # The pre-existing payload shape, hashed the way it was before
    # `variation_key` existed.
    import hashlib
    import json

    expected = hashlib.sha256(
        json.dumps(
            {
                "capability": "tts",
                "provider_name": "KokoroTTSProvider",
                "asset_type": "narration",
                "prompt": "hello world",
                "resolution": None,
                "duration_sec": None,
                "style": None,
                "language": "en",
                "settings": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert key.compute_hash() == expected


# --- prompt composition ------------------------------------------------------


def _beat(narration_text: str = "merchants argued over prices", beat_index: int = 0) -> VisualBeat:
    return VisualBeat(
        segment_id="seg-1",
        order_index=0,
        beat_index=beat_index,
        start_sec=0.0,
        end_sec=4.0,
        narration_text=narration_text,
        requirement_index=0,
        asset_type=AssetType.AI_IMAGE,
        requirement_description="a busy harbour at dawn",
    )


def test_composed_prompt_is_cinematic_not_bare_content():
    prompt = compose_visual_prompt(_beat(), _segment())
    assert "a busy harbour at dawn" in prompt
    for expected in ("lens", "light", "sharp focus", "cinematic composition"):
        assert expected in prompt, f"prompt lacks {expected!r}: {prompt}"


def test_composed_prompt_is_deterministic():
    """The Asset Cache keys on this string — a prompt that varied run to
    run would silently defeat caching entirely.
    """
    beat, segment = _beat(), _segment()
    assert compose_visual_prompt(beat, segment) == compose_visual_prompt(beat, segment)


def test_different_beats_compose_different_prompts():
    segment = _segment()
    first = compose_visual_prompt(_beat("traders crossed the desert", 0), segment)
    second = compose_visual_prompt(_beat("dockworkers hauled heavy sacks", 1), segment)
    assert first != second


def test_camera_framing_and_emotion_actually_change_the_prompt():
    beat = _beat()
    wide = compose_visual_prompt(beat, _segment(camera_framing="wide establishing shot"))
    close = compose_visual_prompt(beat, _segment(camera_framing="extreme close-up"))
    assert wide != close

    calm = compose_visual_prompt(beat, _segment(narration_emotion="reassuring"))
    urgent = compose_visual_prompt(beat, _segment(narration_emotion="urgent"))
    assert calm != urgent


def test_unknown_framing_and_emotion_fall_back_instead_of_failing():
    prompt = compose_visual_prompt(
        _beat(), _segment(camera_framing="whatever", narration_emotion="indescribable")
    )
    assert "lens" in prompt and "light" in prompt


def test_prompt_carries_every_layer_the_brief_calls_for():
    """subject, environment, lighting, camera angle, composition, style,
    and (when applicable) historical accuracy.
    """
    segment = _segment()
    segment = SegmentInput(
        segment_id=segment.segment_id,
        order_index=segment.order_index,
        segment_type=segment.segment_type,
        text=segment.text,
        scene_notes=None,
        visual_notes="A crowded dockside warehouse at first light",
        production=segment.production,
    )
    prompt = compose_visual_prompt(_beat("by 1683 the trade had shifted"), segment)

    assert "a busy harbour at dawn" in prompt  # subject
    assert "set in A crowded dockside warehouse" in prompt  # environment
    assert "light" in prompt  # lighting
    assert "lens" in prompt  # camera angle
    assert "composition" in prompt  # composition
    assert "photorealistic" in prompt  # style
    assert "historically accurate period detail" in prompt  # historical accuracy


def test_environment_comes_from_the_segments_own_scene_notes():
    segment = _segment()
    with_notes = SegmentInput(
        segment_id=segment.segment_id,
        order_index=segment.order_index,
        segment_type=segment.segment_type,
        text=segment.text,
        scene_notes="A candlelit stone cellar packed with barrels",
        visual_notes=None,
        production=segment.production,
    )
    assert "set in A candlelit stone cellar" in compose_visual_prompt(_beat(), with_notes)
    # No notes at all is fine — the layer is simply omitted.
    assert "set in" not in compose_visual_prompt(_beat(), segment)


def test_long_scene_notes_are_trimmed_not_dumped_into_the_prompt():
    segment = _segment()
    verbose = SegmentInput(
        segment_id=segment.segment_id,
        order_index=segment.order_index,
        segment_type=segment.segment_type,
        text=segment.text,
        scene_notes=(
            "A vast candlelit hall crowded with merchants and their servants arguing "
            "loudly over the price of the new season's harvest. Camera pushes in slowly. "
            "Then we cut to the ledger."
        ),
        visual_notes=None,
        production=segment.production,
    )
    prompt = compose_visual_prompt(_beat(), verbose)
    assert "Camera pushes in" not in prompt, "prose beyond the opening clause leaked in"
    assert "set in A vast candlelit hall" in prompt


@pytest.mark.parametrize(
    "narration",
    [
        "by 1683 the siege had ended",
        "in the seventeenth century merchants traded here",
        "medieval guilds controlled the docks",
        "the ancient trade routes carried it north",
    ],
)
def test_historical_beats_request_period_accuracy(narration):
    assert "historically accurate period detail" in compose_visual_prompt(
        _beat(narration), _segment()
    )


@pytest.mark.parametrize(
    "narration",
    [
        "modern roasters use precise heat curves",
        "the app syncs your settings across devices",
        "most people drink it without thinking about any of this",
    ],
)
def test_contemporary_beats_do_not_request_period_accuracy(narration):
    """Asking for period accuracy on a present-day subject is worse than
    asking for nothing — it drags the image toward costume drama.
    """
    assert "period detail" not in compose_visual_prompt(_beat(narration), _segment())


# --- timeline ----------------------------------------------------------------


def _resolved(beat: VisualBeat) -> ResolvedAsset:
    return ResolvedAsset(
        segment_id=beat.segment_id,
        order_index=beat.order_index,
        requirement_index=beat.requirement_index,
        asset_type=beat.asset_type,
        shot_type=None,
        is_audio=False,
        resolution_kind=None,
        asset_id=f"a{beat.beat_index}",
        storage_path=f"p/{beat.segment_id}-{beat.beat_index}.png",
        provider_name="ComfyUIImageProvider",
        description="x",
        beat_index=beat.beat_index,
        start_sec=beat.start_sec,
        duration_sec=beat.duration_sec,
    )


def _built_timeline():
    segments = [_segment("s1", 0), _segment("s2", 1)]
    voices = [_voice("s1", 0, 19.25), _voice("s2", 1, 11.0)]
    beats = VisualBeatPlanningModule().plan(segments, voices)
    resolved = [_resolved(beat) for beat in beats]
    return voices, TimelineBuildingModule().build("proj", segments, resolved, voices, [])


def test_timeline_entries_are_contiguous_with_no_overlap():
    _, timeline = _built_timeline()
    assert timeline.entries[0].start_sec == pytest.approx(0.0)
    for earlier, later in zip(timeline.entries, timeline.entries[1:]):
        assert later.start_sec == pytest.approx(earlier.end_sec)


def test_timeline_duration_equals_total_narration_duration():
    """The video must be exactly as long as what is spoken — no padding
    at the end, and no truncated narration.
    """
    voices, timeline = _built_timeline()
    assert timeline.total_duration_sec == pytest.approx(sum(v.duration_sec for v in voices))


def test_every_segment_receives_at_least_one_visual():
    _, timeline = _built_timeline()
    assert all(entry.visual_assets for entry in timeline.entries)


def test_visuals_within_a_segment_are_ordered_by_beat():
    segments = [_segment("s1", 0)]
    voices = [_voice("s1", 0, 19.25)]
    beats = VisualBeatPlanningModule().plan(segments, voices)
    # Hand them over deliberately shuffled — ordering must be restored
    # from the data, not inherited from resolution order.
    resolved = [_resolved(beat) for beat in reversed(beats)]
    timeline = TimelineBuildingModule().build("proj", segments, resolved, voices, [])
    indices = [asset.beat_index for asset in timeline.entries[0].visual_assets]
    assert indices == sorted(indices)


def test_a_segments_visuals_exactly_fill_its_narration():
    _, timeline = _built_timeline()
    for entry in timeline.entries:
        covered = sum(asset.duration_sec for asset in entry.visual_assets)
        assert covered == pytest.approx(entry.end_sec - entry.start_sec)


# --- compositor timing (real ffmpeg) ----------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_compositor_honors_per_clip_durations_and_never_truncates_narration(tmp_path):
    """Renders a real two-segment video whose visuals have deliberately
    unequal lengths, then checks the output is as long as the narration.

    Uses a small, fast render profile rather than the 1080p production
    one: this test is about the compositor's timing arithmetic (per-clip
    durations, crossfade compensation, the tpad safety net), none of
    which depends on output resolution.
    """
    import io

    from PIL import Image

    from libs.providers.editor.base import EditSegment, EditSpec, VisualClip
    from libs.providers.editor.ffmpeg_provider import FFmpegEditorProvider

    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        "test_small:\n"
        "  resolution: '320x180'\n"
        "  fps: 12\n"
        "  loudness_target_lufs: -14\n"
        "  crossfade_sec: 0.3\n"
        "  subtitle_font_size: 16\n"
        "  ken_burns_zoom_per_sec: 0.015\n"
        "  visual_crossfade_sec: 0.3\n",
        encoding="utf-8",
    )

    def _png(color: str) -> bytes:
        """A *patterned* frame, deliberately not a flat fill: zooming
        into a uniform colour produces byte-identical frames, so a flat
        image could never demonstrate (or fail to demonstrate) the Ken
        Burns move asserted at the end of this test.
        """
        image = Image.new("RGB", (320, 180), color)
        pixels = image.load()
        for x in range(320):
            for y in range(180):
                if (x // 8 + y // 8) % 2 == 0:
                    pixels[x, y] = (255 - (x % 256), (y * 3) % 256, (x + y) % 256)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def _audio(seconds: float, name: str) -> bytes:
        path = tmp_path / name
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={seconds}", "-b:a", "96k", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return path.read_bytes()

    from libs.core.config import get_settings
    from libs.providers.editor import profiles as profiles_module

    settings = get_settings()
    original_path = settings.render_profiles_path
    profiles_module._load_config.cache_clear()
    object.__setattr__(settings, "render_profiles_path", str(profiles))
    try:
        segment_one = EditSegment(
            segment_id="s1", start_sec=0.0, end_sec=9.0,
            visual_clips=[
                VisualClip(data=_png(color), kind="image", duration_sec=duration)
                for color, duration in (("#903030", 2.0), ("#309030", 4.0), ("#303090", 3.0))
            ],
            voice_audio=_audio(9.0, "v1.mp3"),
            transition_type="cut",
        )
        segment_two = EditSegment(
            segment_id="s2", start_sec=9.0, end_sec=14.0,
            visual_clips=[
                VisualClip(data=_png(color), kind="image", duration_sec=duration)
                for color, duration in (("#804060", 2.5), ("#608040", 2.5))
            ],
            voice_audio=_audio(5.0, "v2.mp3"),
            transition_type="cut",
        )
        spec = EditSpec(
            project_id="p",
            segments=[segment_one, segment_two],
            total_duration_sec=14.0,
            profile_name="test_small",
        )
        result = FFmpegEditorProvider(
            config={"ffmpeg_timeout_sec": 240}, api_key=None
        ).render(spec)
    finally:
        object.__setattr__(settings, "render_profiles_path", original_path)
        profiles_module._load_config.cache_clear()

    # The whole point: 14s of narration in, ~14s of video out. Short
    # means truncated narration; long means footage running past the
    # audio, which is what looping used to look like.
    assert result.duration_sec == pytest.approx(14.0, abs=0.5), (
        f"render is {result.duration_sec:.2f}s for 14.0s of narration"
    )
    assert result.resolution == "320x180"

    # Ken Burns: two frames sampled a second apart from *within a single
    # visual* must differ. A still image rendered without motion yields
    # byte-identical frames, which is exactly the static look this is
    # here to prevent — so identical frames mean the effect silently
    # stopped being applied.
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(result.video_bytes)

    def _frame_at(seconds: float) -> bytes:
        frame_path = tmp_path / f"frame_{seconds}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seconds), "-i", str(rendered),
             "-frames:v", "1", "-update", "1", str(frame_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return frame_path.read_bytes()

    # 0.4s and 1.4s both land inside the first visual (0.0-2.0s), well
    # clear of the dissolve into the second.
    assert _frame_at(0.4) != _frame_at(1.4), "still image is motionless — Ken Burns not applied"
