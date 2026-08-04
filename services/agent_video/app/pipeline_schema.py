"""Typed inputs/outputs passed between the Video Agent's six pipeline
modules (see video_agent.py):

    Asset Planning -> Asset Generation -\\
                                          -> Timeline Building -> Rendering
    Voice Generation -> Subtitle Generation -/

Every module takes one of these dataclasses in and returns another out —
no module reaches into another's internals, and no module imports
`libs.providers` capabilities it doesn't itself call (Asset Planning
never touches a provider at all; Rendering never touches one either — it
only reads what earlier modules already resolved). That separation is
what makes a provider swappable independently: changing which class
backs `tts` in config/providers.yaml only touches Voice Generation's own
call site, because every other module only ever sees a `VoiceSegment`
(asset id, storage path, duration, timing) — never the provider that
produced it.

Two modules are the only ones with a real, unavoidable dependency chain
(Asset Generation needs Asset Planning's output; Subtitle Generation
needs Voice Generation's durations/timing; Timeline Building needs all
three of Asset Generation/Voice Generation/Subtitle Generation; Rendering
needs Timeline Building). Everything upstream of Timeline Building works
in *segment-relative* terms (a duration, a 0-based cue time within one
segment) — Timeline Building is deliberately the only place that computes
cumulative, absolute project timing, so that arithmetic exists in exactly
one module instead of being re-derived (and risking drifting out of sync)
in several.
"""

import enum
from dataclasses import dataclass, field

from libs.models.enums import ShotType
from libs.providers.tts.base import WordTiming
from libs.schemas.script_production import (
    VISUAL_ASSET_TYPES,
    AssetType,
    Pacing,
    SegmentProductionMetadata,
    TransitionType,
)

# --- Segment input (loaded once by video_agent.py, fed to every module) ---


@dataclass(frozen=True)
class SegmentInput:
    segment_id: str
    order_index: int
    segment_type: str
    text: str
    scene_notes: str | None
    visual_notes: str | None
    production: SegmentProductionMetadata


@dataclass(frozen=True)
class ChannelBranding:
    """Video-production-relevant subset of `Channel.persona_config` —
    freeform JSONB keys read defensively (all optional; a channel with
    none configured yet still renders, just without branding overlays).
    """

    intro_asset_path: str | None = None
    outro_asset_path: str | None = None
    caption_style: str | None = None
    #: Free-text description of the desired music bed — informational
    #: only; no module resolves it to an actual asset (that would mean
    #: generating/sourcing one, a separate feature this doesn't build).
    music_bed_description: str | None = None
    #: An already-resolved storage path to a real background-music
    #: asset, distinct from the description above — set once a channel
    #: has actually uploaded/generated one. Read by Rendering
    #: (services/agent_video/app/modules/rendering.py) and looped/ducked
    #: under the whole video by the editor provider, the same optional-
    #: with-graceful-fallback treatment as intro/outro.
    music_bed_path: str | None = None


# --- Visual Beat Planning ------------------------------------------------


@dataclass(frozen=True)
class VisualBeat:
    """One distinct visual moment inside a segment.

    A segment's narration is usually far too long to sit under a single
    still image — a 19-second beat holding one frame is what makes a
    generated video read as a slideshow rather than a video. This type is
    the unit that fixes that: Visual Beat Planning subdivides each
    segment's narration into several of these, each covering its own
    slice of the spoken text, and each becoming its own generated visual.

    `start_sec`/`end_sec` are *segment-relative* (0 = the moment this
    segment's narration starts), matching `SubtitleCue`'s convention —
    Timeline Building remains the only module that computes absolute
    project time (see this module's own docstring).
    """

    segment_id: str
    order_index: int
    #: 0-based position within this segment's own beats. Together with
    #: `segment_id` it uniquely identifies a beat across the project,
    #: which is what makes it usable as a cache-variation key (see
    #: `PlannedAsset.variation_key`).
    beat_index: int
    start_sec: float
    end_sec: float
    #: The slice of the segment's narration this beat is on screen for —
    #: what makes each beat's generated visual match what is actually
    #: being said at that moment, rather than the segment as a whole.
    narration_text: str
    #: Which of the segment's `asset_requirements` this beat realizes.
    #: Several beats can share one requirement (a segment usually
    #: declares fewer requirements than it needs visuals for).
    requirement_index: int
    asset_type: AssetType
    #: The requirement's own content description, unmodified — Visual
    #: Prompt Composition combines it with `narration_text` and the
    #: segment's production metadata to build the final prompt.
    requirement_description: str

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


# --- Asset Planning ---------------------------------------------------


class AssetResolutionKind(str, enum.Enum):
    #: Asset Generation must call a `libs.providers` capability to
    #: produce this asset's bytes.
    PROVIDER_GENERATED = "provider_generated"
    #: No separate asset file — Rendering composites this directly from
    #: `PlannedAsset.description` (e.g. burning in overlay text).
    RENDER_TIME_OVERLAY = "render_time_overlay"


#: Routes one Script-Agent `AssetType` to the `libs.providers` capability
#: that satisfies it and the legacy `ShotType` a resolved *visual*
#: requirement collapses to for `StoryboardShot.shot_type`
#: (libs/models/storyboard.py — a narrower, visual-only vocabulary that
#: predates `AssetType`). `AssetType.SUBTITLE_EMPHASIS` is deliberately
#: absent: it is never "planned" as a separate asset at all — Subtitle
#: Generation reads it directly off the segment's own asset requirements
#: as a caption-styling instruction, not something Asset Generation
#: produces a file for.
ASSET_TYPE_ROUTING: dict[AssetType, tuple[str | None, ShotType | None, AssetResolutionKind]] = {
    AssetType.AI_VIDEO: ("video_gen", ShotType.AI_VIDEO, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.ANIMATION: ("video_gen", ShotType.AI_VIDEO, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.AI_IMAGE: ("image_gen", ShotType.AI_IMAGE, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.DIAGRAM: ("image_gen", ShotType.AI_IMAGE, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.MAP: ("image_gen", ShotType.AI_IMAGE, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.PORTRAIT: ("image_gen", ShotType.AI_IMAGE, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.STOCK_FOOTAGE: ("stock_media", ShotType.STOCK, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.TEXT_OVERLAY: (None, ShotType.TEXT_OVERLAY, AssetResolutionKind.RENDER_TIME_OVERLAY),
    AssetType.SOUND_EFFECT: ("audio_library", None, AssetResolutionKind.PROVIDER_GENERATED),
    AssetType.BACKGROUND_MUSIC_CUE: ("audio_library", None, AssetResolutionKind.PROVIDER_GENERATED),
}

#: `AssetType`s that are audio rather than visual — reuses the Script
#: Agent's own visual/audio split (`VISUAL_ASSET_TYPES`) so this module
#: never has to re-derive or risk disagreeing with it. Used by Asset
#: Planning to tag each `PlannedAsset.is_audio`.
AUDIO_ASSET_TYPES = frozenset(AssetType) - VISUAL_ASSET_TYPES


@dataclass(frozen=True)
class PlannedAsset:
    segment_id: str
    order_index: int
    #: Position within the segment's own `asset_requirements` list — a
    #: segment can plan more than one asset (e.g. a stock clip AND a
    #: sound effect), so `(segment_id, requirement_index)` together
    #: identify one planned *requirement* uniquely.
    requirement_index: int
    asset_type: AssetType
    description: str
    resolution_kind: AssetResolutionKind
    #: Which `libs.providers` capability satisfies this — `None` for
    #: `RENDER_TIME_OVERLAY` kinds, which need no provider at all.
    provider_capability: str | None
    shot_type: ShotType | None
    is_audio: bool
    #: Which visual beat within the segment this realizes, and how long
    #: it is on screen — both `None` for assets that aren't per-beat
    #: visuals (segment-wide audio, render-time text overlays), which
    #: still get exactly one planned asset per requirement as before.
    beat_index: int | None = None
    start_sec: float | None = None
    duration_sec: float | None = None
    #: Distinguishes two otherwise-identical provider requests that must
    #: NOT share bytes. The Asset Cache is content-addressed by request
    #: (asset_cache.py), which is right for avoiding repeat work but
    #: wrong for visuals *within one video*: two beats that happen to
    #: produce the same prompt would otherwise collapse onto the same
    #: image file and the video would visibly repeat. Deriving this from
    #: `(segment_id, beat_index)` keeps it deterministic — re-running the
    #: same job still hits the cache — while guaranteeing distinct beats
    #: never do. `None` for non-visual assets, where sharing is correct.
    variation_key: str | None = None


# --- Asset Generation ---------------------------------------------------


@dataclass(frozen=True)
class ResolvedAsset:
    segment_id: str
    order_index: int
    requirement_index: int
    asset_type: AssetType
    shot_type: ShotType | None
    is_audio: bool
    resolution_kind: AssetResolutionKind
    #: `None` for a `RENDER_TIME_OVERLAY` entry (no file was produced —
    #: `description` is composited directly at render time instead).
    asset_id: str | None
    storage_path: str | None
    #: Which concrete provider produced it (e.g. "stub", "pexels") — for
    #: `Asset.provider`. `None` for `RENDER_TIME_OVERLAY` entries.
    provider_name: str | None
    description: str
    #: Carried through unchanged from the `PlannedAsset` this resolves,
    #: so Timeline Building can order a segment's visuals by beat and
    #: Rendering can give each one its own on-screen duration instead of
    #: dividing the segment evenly. `None` for non-per-beat assets.
    beat_index: int | None = None
    start_sec: float | None = None
    duration_sec: float | None = None


# --- Voice Generation ---------------------------------------------------


@dataclass(frozen=True)
class VoiceSegment:
    segment_id: str
    order_index: int
    asset_id: str
    storage_path: str
    duration_sec: float
    #: `None` when the configured TTS provider doesn't return per-word
    #: timing — Subtitle Generation falls back to an even split of
    #: `duration_sec` across the segment's words instead of requiring this.
    word_timings: list[WordTiming] | None
    provider_name: str | None


# --- Subtitle Generation -------------------------------------------------


@dataclass(frozen=True)
class SubtitleCue:
    segment_id: str
    order_index: int
    #: Segment-relative (0 = the moment this segment's audio starts) —
    #: Timeline Building shifts these to absolute project time.
    start_sec: float
    end_sec: float
    text: str
    #: True if this cue should render with emphasis styling — from either
    #: the segment's `emphasis_words` overlapping this cue's text, or a
    #: `subtitle_emphasis` asset requirement on the segment.
    emphasized: bool = False


# --- Timeline Building ---------------------------------------------------


@dataclass(frozen=True)
class TimelineEntry:
    segment_id: str
    order_index: int
    #: Absolute project time (seconds from the start of the final video).
    start_sec: float
    end_sec: float
    #: The narration audio driving this entry's duration — a
    #: `VoiceSegment`, not a `ResolvedAsset`: it comes from Voice
    #: Generation, a different module than Asset Generation, and carries
    #: `word_timings` a plain resolved asset has no concept of.
    voice_asset: VoiceSegment
    visual_assets: list[ResolvedAsset]
    supplementary_audio: list[ResolvedAsset]
    subtitle_cues: list[SubtitleCue]
    transition_type: TransitionType
    pacing: Pacing
    camera_framing: str


@dataclass(frozen=True)
class Timeline:
    project_id: str
    entries: list[TimelineEntry] = field(default_factory=list)
    total_duration_sec: float = 0.0


# --- Rendering ---------------------------------------------------------


@dataclass(frozen=True)
class RenderResult:
    asset_id: str
    storage_path: str
    duration_sec: float
    resolution: str
    render_engine: str
