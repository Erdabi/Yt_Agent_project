"""Timeline Building module.

Fifth stage of the Video Agent's pipeline (see video_agent.py): the
single place that computes cumulative, absolute project timing. Every
upstream module (Asset Generation, Voice Generation, Subtitle Generation)
works in segment-relative terms — a duration, a 0-based cue time within
one segment — precisely so this arithmetic exists in exactly one place
instead of being re-derived (and risking drifting out of sync) in
several.

    Input:  list[SegmentInput], list[ResolvedAsset], list[VoiceSegment],
            list[SubtitleCue]
    Output: Timeline

A segment's narration audio is the authoritative driver of its length —
visuals get stretched/looped/trimmed to fill whatever duration Voice
Generation produced, not the other way around, since narration is what a
viewer actually experiences moment-to-moment. This module makes no
provider calls and touches no external system: it is pure data assembly,
fully real and testable regardless of which providers happen to be
configured for anything upstream.
"""

import dataclasses

from libs.core.logging import get_logger

from ..pipeline_schema import ResolvedAsset, SegmentInput, SubtitleCue, Timeline, TimelineEntry, VoiceSegment

logger = get_logger(__name__)


class TimelineBuildingModule:
    def build(
        self,
        project_id: str,
        segments: list[SegmentInput],
        resolved_assets: list[ResolvedAsset],
        voice_segments: list[VoiceSegment],
        subtitle_cues: list[SubtitleCue],
    ) -> Timeline:
        voice_by_segment = {voice.segment_id: voice for voice in voice_segments}
        assets_by_segment: dict[str, list[ResolvedAsset]] = {}
        for asset in resolved_assets:
            assets_by_segment.setdefault(asset.segment_id, []).append(asset)
        cues_by_segment: dict[str, list[SubtitleCue]] = {}
        for cue in subtitle_cues:
            cues_by_segment.setdefault(cue.segment_id, []).append(cue)

        entries: list[TimelineEntry] = []
        cursor_sec = 0.0
        for segment in sorted(segments, key=lambda s: s.order_index):
            voice_segment = voice_by_segment.get(segment.segment_id)
            if voice_segment is None:
                raise ValueError(
                    f"segment {segment.segment_id} (order {segment.order_index}) has no "
                    "voice segment — Timeline Building requires narration timing for every "
                    "segment to place it in the project timeline"
                )

            start_sec = cursor_sec
            end_sec = start_sec + voice_segment.duration_sec

            segment_assets = assets_by_segment.get(segment.segment_id, [])
            # Ordered by visual beat: a segment now holds several visuals
            # in sequence (Visual Beat Planning), and the order they were
            # resolved in is not necessarily the order they appear on
            # screen. Assets with no beat (a render-time text overlay,
            # which spans the whole segment) sort last, after the
            # beat-sequenced visuals they overlay.
            visual_assets = sorted(
                (asset for asset in segment_assets if not asset.is_audio),
                key=lambda asset: (
                    asset.beat_index is None,
                    asset.beat_index if asset.beat_index is not None else 0,
                ),
            )
            supplementary_audio = [asset for asset in segment_assets if asset.is_audio]

            shifted_cues = [
                dataclasses.replace(
                    cue, start_sec=cue.start_sec + start_sec, end_sec=cue.end_sec + start_sec
                )
                for cue in cues_by_segment.get(segment.segment_id, [])
            ]

            entries.append(
                TimelineEntry(
                    segment_id=segment.segment_id,
                    order_index=segment.order_index,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    voice_asset=voice_segment,
                    visual_assets=visual_assets,
                    supplementary_audio=supplementary_audio,
                    subtitle_cues=shifted_cues,
                    transition_type=segment.production.transition_type,
                    pacing=segment.production.pacing,
                    camera_framing=segment.production.camera_framing,
                )
            )
            cursor_sec = end_sec

        logger.info(
            "timeline_building_complete",
            project_id=project_id,
            entry_count=len(entries),
            total_duration_sec=cursor_sec,
        )
        return Timeline(project_id=project_id, entries=entries, total_duration_sec=cursor_sec)
