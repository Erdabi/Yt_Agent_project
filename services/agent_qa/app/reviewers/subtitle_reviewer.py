"""SubtitleReviewer — timing, readability, overlap, and missing-caption
checks for a project's burned-in subtitles.

Subtitle cues are never persisted (they're computed in-memory and burned
directly into the video by the ffmpeg compositor — see
services/agent_video/app/modules/subtitle_generation.py's own docstring),
so this reviewer re-derives them from the same already-persisted inputs
that module uses (each segment's word timings, from `Voiceover`/
`Asset.metadata_`) using the identical chunking rule. This is a
deliberate, small, bounded duplication of that module's algorithm rather
than an import: services/agent_qa and services/agent_video are separate
deployable containers, each shipping only its own code plus `libs/` (see
each service's own Dockerfile) — the same service-boundary tradeoff this
codebase already makes elsewhere (e.g. neither service imports the
other's Asset Cache).

Deliberately has no LLM call, unlike Script/Video/Thumbnail: every check
asked for here (timing, readability, overlap, missing captions) is a
plain arithmetic check over cue start/end times and word counts — there
is no qualitative judgment left for a model to make.
"""

from dataclasses import dataclass

from libs.core.logging import get_logger

from ..qa_schema import Issue, ReviewInput, ReviewResult, SegmentData, build_review_result
from .base import Reviewer

logger = get_logger(__name__)

_CATEGORY = "subtitles"
#: Mirrors services/agent_video/app/modules/subtitle_generation.py's
#: `_MAX_WORDS_PER_CUE` — how many words render in one caption cue.
_MAX_WORDS_PER_CUE = 6
_MIN_CUE_DURATION_SEC = 0.3
_MAX_CUE_DURATION_SEC = 7.0
#: Above this reading speed, a viewer can't comfortably read the cue
#: before it disappears.
_MAX_READABLE_WORDS_PER_SEC = 4.0


@dataclass(frozen=True)
class _DerivedCue:
    segment_order_index: int
    start_sec: float
    end_sec: float
    word_count: int


class SubtitleReviewer(Reviewer):
    category = _CATEGORY

    def review(self, review_input: ReviewInput) -> ReviewResult:
        issues: list[Issue] = []
        cues: list[_DerivedCue] = []

        cumulative_offset = 0.0
        for segment in sorted(review_input.segments, key=lambda s: s.order_index):
            if segment.voiceover is None:
                # Already reported as a missing-narration issue by
                # AudioReviewer — still worth its own subtitles-category
                # finding, since a segment with no narration has no
                # captions either, a distinct dimension of the same gap.
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="high",
                        detail=f"Segment {segment.order_index} has no narration, so it has no captions either.",
                        suggested_fix="Re-run Voice Generation for this segment before checking subtitles again.",
                    )
                )
                continue

            segment_cues = self._cues_for_segment(segment, cumulative_offset)
            if not segment_cues:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="high",
                        detail=f"Segment {segment.order_index} produced no subtitle cues at all.",
                        suggested_fix="Check that this segment's narration text/word timings are non-empty.",
                    )
                )
            cues.extend(segment_cues)
            cumulative_offset += segment.voiceover.duration_sec

        issues.extend(self._timing_and_readability_issues(cues))
        issues.extend(self._overlap_issues(cues))

        summary = (
            "No subtitle problems found." if not issues
            else f"{len(issues)} subtitle issue(s) found — see details."
        )
        return build_review_result(_CATEGORY, summary, issues)

    @staticmethod
    def _cues_for_segment(segment: SegmentData, cumulative_offset: float) -> list[_DerivedCue]:
        word_timings = segment.voiceover.word_timings if segment.voiceover else []
        cues: list[_DerivedCue] = []
        for chunk_start in range(0, len(word_timings), _MAX_WORDS_PER_CUE):
            chunk = word_timings[chunk_start : chunk_start + _MAX_WORDS_PER_CUE]
            if not chunk:
                continue
            cues.append(
                _DerivedCue(
                    segment_order_index=segment.order_index,
                    start_sec=cumulative_offset + chunk[0].start_sec,
                    end_sec=cumulative_offset + chunk[-1].end_sec,
                    word_count=len(chunk),
                )
            )
        return cues

    @staticmethod
    def _timing_and_readability_issues(cues: list[_DerivedCue]) -> list[Issue]:
        issues: list[Issue] = []
        for cue in cues:
            duration = cue.end_sec - cue.start_sec
            if duration < _MIN_CUE_DURATION_SEC:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="low",
                        detail=(
                            f"A cue in segment {cue.segment_order_index} at {cue.start_sec:.1f}s "
                            f"lasts only {duration:.2f}s — too brief to read."
                        ),
                        suggested_fix="Slow down narration pacing for this segment, or use fewer words per cue.",
                    )
                )
            elif duration > _MAX_CUE_DURATION_SEC:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="low",
                        detail=(
                            f"A cue in segment {cue.segment_order_index} at {cue.start_sec:.1f}s "
                            f"lasts {duration:.1f}s — unusually long for one caption."
                        ),
                        suggested_fix="Split this stretch of narration into more, shorter cues.",
                    )
                )
                continue
            else:
                duration_for_rate = duration
                if duration_for_rate > 0 and cue.word_count / duration_for_rate > _MAX_READABLE_WORDS_PER_SEC:
                    issues.append(
                        Issue(
                            category=_CATEGORY,
                            severity="medium",
                            detail=(
                                f"A cue in segment {cue.segment_order_index} at {cue.start_sec:.1f}s "
                                f"reads at {cue.word_count / duration_for_rate:.1f} words/sec — too fast to read comfortably."
                            ),
                            suggested_fix="Slow down narration pacing or shorten this cue's text.",
                        )
                    )
        return issues

    @staticmethod
    def _overlap_issues(cues: list[_DerivedCue]) -> list[Issue]:
        ordered = sorted(cues, key=lambda cue: cue.start_sec)
        issues: list[Issue] = []
        for previous_cue, next_cue in zip(ordered, ordered[1:]):
            if next_cue.start_sec < previous_cue.end_sec:
                issues.append(
                    Issue(
                        category=_CATEGORY,
                        severity="medium",
                        detail=(
                            f"A cue in segment {previous_cue.segment_order_index} ending at "
                            f"{previous_cue.end_sec:.1f}s overlaps the next cue (segment "
                            f"{next_cue.segment_order_index}) starting at {next_cue.start_sec:.1f}s."
                        ),
                        suggested_fix="Adjust cue timing so consecutive captions don't overlap on screen.",
                    )
                )
        return issues
