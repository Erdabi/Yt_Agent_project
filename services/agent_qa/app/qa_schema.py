"""Shared types for the Quality Control Agent (quality_control_agent.py)
and its reviewers (reviewers/).

    ReviewInput  — everything a reviewer might need, gathered once by
                   `QualityControlAgent._gather` so no reviewer queries
                   the database or storage itself.
    ReviewResult — one reviewer's verdict: a category, a computed
                   pass/fail, a summary, and itemized `Issue`s.

`ReviewResult.passed` is never set by hand — `build_review_result`
derives it uniformly from issue severities (any "high" severity issue
fails that category) so every reviewer's pass/fail follows the same rule
instead of five reviewers each hand-rolling their own threshold.
"""

from dataclasses import dataclass, field
from typing import Literal

from libs.context import ProjectContext
from libs.providers.tts.base import WordTiming
from libs.schemas.script_production import SegmentProductionMetadata

Severity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Issue:
    category: str
    severity: Severity
    detail: str
    suggested_fix: str


@dataclass(frozen=True)
class ReviewResult:
    category: str
    passed: bool
    summary: str
    issues: list[Issue] = field(default_factory=list)


def build_review_result(category: str, summary: str, issues: list[Issue]) -> ReviewResult:
    passed = not any(issue.severity == "high" for issue in issues)
    return ReviewResult(category=category, passed=passed, summary=summary, issues=issues)


# --- Gathered production data (read once by QualityControlAgent._gather) ---


@dataclass(frozen=True)
class StoryboardShotData:
    shot_type: str
    prompt_or_query: str
    asset_id: str | None
    order_index: int


@dataclass(frozen=True)
class VoiceoverData:
    asset_id: str
    storage_path: str
    duration_sec: float
    provider: str | None
    model: str | None
    voice_id: str | None
    language: str | None
    word_timings: list[WordTiming]


@dataclass(frozen=True)
class SegmentData:
    segment_id: str
    order_index: int
    segment_type: str
    text: str
    production_metadata: SegmentProductionMetadata
    #: `None` when Voice Generation never ran for this segment (e.g. the
    #: video job failed before reaching it) — a genuine finding, not a
    #: gathering error.
    voiceover: VoiceoverData | None
    storyboard_shots: list[StoryboardShotData]


@dataclass(frozen=True)
class RenderData:
    asset_id: str
    storage_path: str
    #: Downloaded to a local temp path once by `QualityControlAgent`
    #: (both VideoReviewer and AudioReviewer run their own ffprobe/ffmpeg
    #: subprocess commands against it) — cleaned up when the agent's job
    #: finishes, not any one reviewer's responsibility.
    local_path: str
    resolution: str | None
    duration_sec: float | None
    render_engine: str | None
    profile_name: str | None


@dataclass(frozen=True)
class ThumbnailData:
    asset_id: str
    storage_path: str
    #: Read into memory once (not a temp path) — its only consumer is
    #: `ThumbnailReviewer`'s single vision LLM call, not a subprocess tool.
    image_bytes: bytes
    variant_label: str | None
    is_selected: bool
    prompt: str | None
    concept_name: str | None
    overlay_text: str | None
    provider: str | None


@dataclass(frozen=True)
class ReviewInput:
    project_context: ProjectContext
    video_title: str
    script_content: str
    script_target_duration_sec: int | None
    segments: list[SegmentData]
    #: `None` when Rendering never produced a render (e.g. the video job
    #: failed before reaching it) — VideoReviewer/AudioReviewer both
    #: treat that as a hard, high-severity finding of their own rather
    #: than crashing.
    render: RenderData | None
    #: `None` when the Thumbnail Agent never produced one.
    thumbnail: ThumbnailData | None
