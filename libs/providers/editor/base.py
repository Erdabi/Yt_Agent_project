"""Video editor / compositor provider interface.

A concrete implementation turns a fully-resolved edit plan — visual
clips, narration and supplementary audio, an optional whole-video music
bed, burned-in subtitle cues, an optional intro/outro — into a final
rendered video, for the Video Agent's Rendering module
(services/agent_video/app/modules/rendering.py). Rendering itself builds
the plan (`EditSpec` below) from a `Timeline`; a concrete provider only
ever sees this generic shape, never `pipeline_schema.py`'s own
dataclasses, so libs/providers stays independent of any one service.

Unlike every other capability in libs/providers (image_gen/video_gen/
tts/stock_media/audio_library), which stand in for a paid creative
vendor with no real implementation yet, a compositor needs no vendor
account at all — ffmpeg is a free, local binary — so this capability's
real provider (ffmpeg_provider.py) is a genuine, working implementation
rather than a stub standing in for one.
"""

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from libs.providers.base import Provider


@dataclass(frozen=True)
class VisualClip:
    """One visual shown during a segment. `kind` distinguishes a static
    image (held for its slice of the segment's duration) from a video
    clip (trimmed, or extended by holding its final frame, to fill it) —
    a compositor needs to treat the two differently.
    """

    data: bytes
    kind: str  # "image" | "video"
    #: How long this clip holds the screen. Set by the caller from the
    #: visual beat this clip realizes, so a segment's visuals can have
    #: genuinely different lengths (a beat is sized from the narration it
    #: covers, and narration slices are not uniform). `None` means the
    #: caller has no per-clip timing, in which case a provider divides
    #: the segment evenly across its clips — the pre-beat behavior, kept
    #: so a caller that doesn't compute beats still renders correctly.
    duration_sec: float | None = None


@dataclass(frozen=True)
class EditSegment:
    """One segment's worth of material to composite, already placed on
    the project timeline by Timeline Building — `start_sec`/`end_sec` are
    absolute project time.
    """

    segment_id: str
    start_sec: float
    end_sec: float
    visual_clips: list[VisualClip] = field(default_factory=list)
    #: Static on-screen text for a `text_overlay` requirement — shown for
    #: this segment's whole duration, distinct from the timed subtitle
    #: cues on `EditSpec` below.
    overlay_texts: list[str] = field(default_factory=list)
    voice_audio: bytes = b""
    #: Per-segment incidental audio (sound effects, a localized musical
    #: sting) — distinct from `EditSpec.background_music` below, which
    #: spans the whole video rather than one beat.
    supplementary_audio: list[bytes] = field(default_factory=list)
    #: How this segment transitions into the *next* one — "cut", "fade",
    #: "dissolve", "wipe", "zoom", "slide", or "match_cut" (mirrors
    #: libs.schemas.script_production.TransitionType's values). A
    #: concrete provider is free to collapse types it doesn't implement
    #: distinctly down to a simpler treatment (e.g. any non-cut
    #: transition becoming a plain cross-fade) rather than requiring
    #: every provider to implement all seven distinctly.
    transition_type: str = "cut"

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class SubtitleLine:
    """A caption cue, timed in absolute project seconds (matching
    `EditSegment.start_sec`/`end_sec`) so a provider can burn in every
    cue in one pass over the fully assembled video rather than
    per-segment.
    """

    start_sec: float
    end_sec: float
    text: str
    emphasized: bool = False


@dataclass(frozen=True)
class EditSpec:
    project_id: str
    segments: list[EditSegment]
    total_duration_sec: float
    subtitle_lines: list[SubtitleLine] = field(default_factory=list)
    caption_style: str | None = None
    #: Pre-made branding clips (see ChannelBranding — optional, `None`
    #: when a channel hasn't configured one), assumed to already be
    #: complete, self-contained video files.
    intro_asset: bytes | None = None
    outro_asset: bytes | None = None
    #: A continuous music bed spanning the whole video (optional, `None`
    #: when a channel hasn't configured one) — looped/trimmed to
    #: `total_duration_sec` and ducked under narration/supplementary
    #: audio, distinct from any one segment's own
    #: `EditSegment.supplementary_audio`.
    background_music: bytes | None = None
    #: Which named `RenderProfile` (libs/providers/editor/profiles.py)
    #: to composite against — resolution, frame rate, loudness target,
    #: crossfade duration, subtitle sizing all travel together as one
    #: reusable, swappable bundle rather than loose parameters, so a
    #: future output format (Shorts, a different aspect ratio) is a new
    #: named profile, not a rewrite of the compositor.
    profile_name: str = "long_form_1080p"


@dataclass(frozen=True)
class EditResult:
    video_bytes: bytes
    duration_sec: float
    resolution: str


@dataclass(frozen=True)
class RenderProgress:
    """One structured progress update from a provider's `render()` call.
    `stage` is a short, stable machine-readable name (e.g.
    "normalize_segments", "mix_background_music", "finalize") a caller
    can log or aggregate without parsing free text; `message` is the
    human-readable detail for that same event.
    """

    stage: str
    current_step: int
    total_steps: int
    message: str = ""

    @property
    def percent(self) -> float:
        return (self.current_step / self.total_steps) * 100 if self.total_steps else 0.0


#: A callback a caller passes into `render()` to receive `RenderProgress`
#: events as they happen — e.g. to log them (see RenderingModule) or, in
#: the future, persist them somewhere pollable. Never required: a
#: provider must work correctly with `on_progress=None`.
ProgressCallback = Callable[[RenderProgress], None]


class EditorError(RuntimeError):
    """Base class for every error an `EditorProvider` raises — lets a
    caller catch `EditorError` generically without needing to know which
    concrete compositor is configured, the same reason this interface
    exists at all.
    """


class EditorInputError(EditorError):
    """Something wrong with the *input* — a missing or unreadable asset,
    corrupt media bytes a compositor's own probing step rejects outright,
    an empty timeline. Distinct from `EditorRenderError`: retrying the
    identical input won't help, so a caller (or a human) needs to fix the
    input before trying again, not just wait and retry.
    """


class EditorTimeoutError(EditorError):
    """A compositor subprocess exceeded its configured time budget.
    Distinguished from `EditorRenderError` so a caller could, in
    principle, choose to retry with a longer timeout — though today the
    Manager just retries the whole Video Agent job like any other
    failure (docs/architecture/03-agent-responsibilities.md §3.4.6).
    """


class EditorRenderError(EditorError):
    """The compositor itself failed for a reason other than a timeout —
    an unsupported codec, a real ffmpeg bug, disk exhaustion. Carries
    whatever diagnostic detail (stage, stderr) the concrete provider can
    attach, so a failure is attributable without re-running it with
    extra logging turned on.
    """


class EditorProvider(Provider):
    @abstractmethod
    def render(self, spec: EditSpec, *, on_progress: ProgressCallback | None = None) -> EditResult:
        """Composite `spec` into a final video and return its bytes plus
        the actual resulting duration/resolution (which may differ
        slightly from what was requested — e.g. rounded to whole
        frames). Reports structured progress through `on_progress`, when
        given. Raises `EditorInputError`/`EditorTimeoutError`/
        `EditorRenderError` on failure — never a bare, provider-specific
        exception.
        """
