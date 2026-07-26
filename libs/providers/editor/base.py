"""Video editor / compositor provider interface.

A concrete implementation turns a fully-resolved edit plan — visual
clips, narration and supplementary audio, burned-in subtitle cues, an
optional intro/outro — into a final rendered video, for the Video
Agent's Rendering module
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
from dataclasses import dataclass, field

from libs.providers.base import Provider


@dataclass(frozen=True)
class VisualClip:
    """One visual shown during a segment. `kind` distinguishes a static
    image (looped to fill its slice of the segment's duration) from a
    video clip (trimmed/looped to fill it) — a compositor needs to treat
    the two differently.
    """

    data: bytes
    kind: str  # "image" | "video"


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
    resolution: str = "1920x1080"


@dataclass(frozen=True)
class EditResult:
    video_bytes: bytes
    duration_sec: float
    resolution: str


class EditorProvider(Provider):
    @abstractmethod
    def render(self, spec: EditSpec) -> EditResult:
        """Composite `spec` into a final video and return its bytes plus
        the actual resulting duration/resolution (which may differ
        slightly from what was requested — e.g. rounded to whole
        frames).
        """
