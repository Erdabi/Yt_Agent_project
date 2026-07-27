"""AudioReviewer — missing narration, clipping, silence, timing
mismatches, and synchronization checks for a project's rendered video.

Deliberately has no LLM call, unlike Script/Video/Thumbnail: every check
the Quality Control Agent's spec asks for in this category (missing
narration, clipping, silence, timing mismatches, synchronization) is
objectively measurable from the persisted `Voiceover` rows and the
render's own audio track (../media_inspection.py) — there is no
inherently qualitative judgment left for a model to make here, so adding
one would just be an unnecessary provider call, not a real capability.
"""

from libs.core.logging import get_logger

from ..media_inspection import detect_silence, measure_peak_level_db, probe_streams
from ..qa_schema import Issue, ReviewInput, ReviewResult, build_review_result
from .base import Reviewer

logger = get_logger(__name__)

_CATEGORY = "audio"
#: A peak this close to 0 dBFS (full scale) is a real clipping signal —
#: cleanly-mixed audio essentially never touches full scale.
_CLIPPING_PEAK_THRESHOLD_DB = -0.5
#: How far the render's total duration may drift from the sum of every
#: segment's own narration duration before it's flagged as a timing
#: mismatch — a generous band to allow for intro/outro/crossfades.
_TOTAL_DURATION_TOLERANCE_RATIO = 0.25
#: How far apart the render's audio and video stream durations may be
#: before it's flagged as a synchronization/mux problem.
_STREAM_DURATION_SYNC_TOLERANCE_SEC = 1.0


class AudioReviewer(Reviewer):
    category = _CATEGORY

    def review(self, review_input: ReviewInput) -> ReviewResult:
        issues: list[Issue] = []
        issues.extend(self._missing_narration_issues(review_input))

        if review_input.render is None:
            issues.append(
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail="No rendered video exists for this project — its audio track can't be checked.",
                    suggested_fix="Re-run the Video Agent's Rendering stage before publishing.",
                )
            )
            return build_review_result(_CATEGORY, "Audio could not be checked: no render exists.", issues)

        local_path = review_input.render.local_path
        issues.extend(self._clipping_issues(local_path))
        issues.extend(self._silence_issues(local_path))
        issues.extend(self._timing_mismatch_issues(review_input))
        issues.extend(self._synchronization_issues(local_path))

        summary = (
            "No audio problems found." if not issues
            else f"{len(issues)} audio issue(s) found — see details."
        )
        return build_review_result(_CATEGORY, summary, issues)

    @staticmethod
    def _missing_narration_issues(review_input: ReviewInput) -> list[Issue]:
        return [
            Issue(
                category=_CATEGORY,
                severity="high",
                detail=f"Segment {segment.order_index} ({segment.segment_type}) has no narration audio.",
                suggested_fix="Re-run Voice Generation for this segment.",
            )
            for segment in review_input.segments
            if segment.voiceover is None
        ]

    @staticmethod
    def _clipping_issues(local_path: str) -> list[Issue]:
        peak_db = measure_peak_level_db(local_path)
        if peak_db is not None and peak_db >= _CLIPPING_PEAK_THRESHOLD_DB:
            return [
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail=f"The mixed audio peaks at {peak_db:.2f} dBFS — this is audible clipping.",
                    suggested_fix="Reduce narration/background-music gain and re-render.",
                )
            ]
        return []

    @staticmethod
    def _silence_issues(local_path: str) -> list[Issue]:
        gaps = detect_silence(local_path)
        return [
            Issue(
                category=_CATEGORY,
                severity="medium",
                detail=f"A silence gap of {end_sec - start_sec:.1f}s was detected from {start_sec:.1f}s to {end_sec:.1f}s.",
                suggested_fix="Check narration/background-music coverage for that time range.",
            )
            for start_sec, end_sec in gaps
        ]

    @staticmethod
    def _timing_mismatch_issues(review_input: ReviewInput) -> list[Issue]:
        if review_input.render is None or review_input.render.duration_sec is None:
            return []
        total_voiceover_sec = sum(
            segment.voiceover.duration_sec for segment in review_input.segments if segment.voiceover
        )
        if total_voiceover_sec <= 0:
            return []
        render_sec = review_input.render.duration_sec
        lower_bound = total_voiceover_sec * (1 - _TOTAL_DURATION_TOLERANCE_RATIO)
        upper_bound = total_voiceover_sec * (1 + _TOTAL_DURATION_TOLERANCE_RATIO)
        if lower_bound <= render_sec <= upper_bound:
            return []
        return [
            Issue(
                category=_CATEGORY,
                severity="medium",
                detail=(
                    f"The render is {render_sec:.1f}s but the segments' narration totals "
                    f"{total_voiceover_sec:.1f}s — a timing mismatch beyond intro/outro/crossfade overhead."
                ),
                suggested_fix="Re-check Timeline Building/Rendering for a duration-computation bug.",
            )
        ]

    @staticmethod
    def _synchronization_issues(local_path: str) -> list[Issue]:
        probe = probe_streams(local_path)
        video_stream = probe.video_stream()
        audio_stream = probe.audio_stream()
        if video_stream is None or audio_stream is None:
            missing = "video" if video_stream is None else "audio"
            return [
                Issue(
                    category=_CATEGORY,
                    severity="high",
                    detail=f"The render has no {missing} stream at all.",
                    suggested_fix="Re-render the video — the output is missing a required stream.",
                )
            ]
        if video_stream.duration_sec is None or audio_stream.duration_sec is None:
            return []
        drift = abs(video_stream.duration_sec - audio_stream.duration_sec)
        if drift <= _STREAM_DURATION_SYNC_TOLERANCE_SEC:
            return []
        return [
            Issue(
                category=_CATEGORY,
                severity="medium",
                detail=(
                    f"The video stream is {video_stream.duration_sec:.1f}s but the audio "
                    f"stream is {audio_stream.duration_sec:.1f}s — a {drift:.1f}s sync/mux drift."
                ),
                suggested_fix="Re-render — the audio and video tracks were not muxed to matching lengths.",
            )
        ]
