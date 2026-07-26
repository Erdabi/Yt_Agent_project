"""Subtitle Generation module.

Runs after Voice Generation inside the Video Agent's pipeline (see
video_agent.py) — caption timing needs the real (or estimated) per-word
timing Voice Generation already produced, so this module has a genuine
dependency on it, unlike most of this pipeline's sequencing.

    Input:  list[SegmentInput], list[VoiceSegment]
    Output: list[SubtitleCue]

Every cue's `start_sec`/`end_sec` is *segment-relative* — 0 means "the
moment this segment's audio starts"; Timeline Building is the only
module that shifts these to absolute project time (see
pipeline_schema.py's module docstring for why). Uses a provider's real
per-word timestamps when Voice Generation returned them; otherwise
evenly distributes the segment's known duration across its words. Either
way, this module makes no provider calls of its own and doesn't care
which TTS vendor is configured — it only reads what Voice Generation
already resolved.
"""

from libs.core.logging import get_logger
from libs.schemas.script_production import AssetType

from ..pipeline_schema import SegmentInput, SubtitleCue, VoiceSegment

logger = get_logger(__name__)

#: How many words render in one caption cue at once — a common subtitle
#: convention keeping a cue short enough to read at a glance.
_MAX_WORDS_PER_CUE = 6


class SubtitleGenerationModule:
    def generate(
        self, segments: list[SegmentInput], voice_segments: list[VoiceSegment]
    ) -> list[SubtitleCue]:
        voice_by_segment = {voice.segment_id: voice for voice in voice_segments}
        cues: list[SubtitleCue] = []
        for segment in segments:
            voice_segment = voice_by_segment.get(segment.segment_id)
            if voice_segment is None:
                continue
            cues.extend(self._cues_for_segment(segment, voice_segment))
        logger.info("subtitle_generation_complete", segment_count=len(segments), cue_count=len(cues))
        return cues

    def _cues_for_segment(
        self, segment: SegmentInput, voice_segment: VoiceSegment
    ) -> list[SubtitleCue]:
        words_with_timing = self._word_timings(segment, voice_segment)
        if not words_with_timing:
            return []

        emphasis_words = {word.lower() for word in segment.production.emphasis_words}
        # A `subtitle_emphasis` asset requirement is a segment-wide
        # styling instruction (from the Script Agent), not tied to any
        # one word — every cue in this segment gets emphasis styling.
        segment_wide_emphasis = any(
            requirement.asset_type == AssetType.SUBTITLE_EMPHASIS
            for requirement in segment.production.asset_requirements
        )

        cues: list[SubtitleCue] = []
        for chunk_start in range(0, len(words_with_timing), _MAX_WORDS_PER_CUE):
            chunk = words_with_timing[chunk_start : chunk_start + _MAX_WORDS_PER_CUE]
            words = [word for word, _, _ in chunk]
            emphasized = segment_wide_emphasis or any(
                word.strip(".,!?\"'").lower() in emphasis_words for word in words
            )
            cues.append(
                SubtitleCue(
                    segment_id=segment.segment_id,
                    order_index=segment.order_index,
                    start_sec=chunk[0][1],
                    end_sec=chunk[-1][2],
                    text=" ".join(words),
                    emphasized=emphasized,
                )
            )
        return cues

    @staticmethod
    def _word_timings(
        segment: SegmentInput, voice_segment: VoiceSegment
    ) -> list[tuple[str, float, float]]:
        if voice_segment.word_timings:
            return [(wt.word, wt.start_sec, wt.end_sec) for wt in voice_segment.word_timings]

        words = segment.text.split()
        if not words:
            return []
        per_word = voice_segment.duration_sec / len(words)
        return [(word, index * per_word, (index + 1) * per_word) for index, word in enumerate(words)]
