"""Visual Beat Planning module.

Runs after Voice Generation and before Asset Planning inside the Video
Agent's pipeline (see video_agent.py): decides *how many* visuals each
segment needs and *which words each one covers*.

    Input:  list[SegmentInput], list[VoiceSegment]
    Output: list[VisualBeat]

Why this module exists
----------------------
The Script Agent declares what a beat should show, but it declares it
once per narrative beat — and a narrative beat is routinely 15-20
seconds of narration. Realizing that as one asset meant one still image
held for the segment's entire duration, which is what makes a generated
video read as a slideshow rather than a video. Nothing upstream can fix
that: the Script Agent writes narration, not shot lists, and asking it
to also emit a per-3-seconds shot breakdown would both bloat its output
schema and make the visual rhythm depend on a language model's sense of
timing rather than on the narration's *measured* length.

So the split happens here instead, from real data: Voice Generation has
already synthesized the audio, so this module knows exactly how long
each segment actually is (and, when the TTS provider supplies word-level
timing, exactly when each word is spoken). Segment length divided by a
target seconds-per-visual gives the beat count; the narration text is
then apportioned across those beats so each one's generated visual
matches what is being said while it is on screen.

Like Asset Planning and Timeline Building, this module makes no provider
calls and writes nothing — it is pure computation over data already in
memory, so it is fully real and testable regardless of which providers
are configured.
"""

import math

from libs.core.logging import get_logger
from libs.providers.editor.profiles import (
    DEFAULT_PROFILE_NAME,
    RenderProfile,
    get_render_profile,
)
from libs.schemas.script_production import VISUAL_ASSET_TYPES, AssetRequirement, Pacing

from ..pipeline_schema import (
    ASSET_TYPE_ROUTING,
    AssetResolutionKind,
    SegmentInput,
    VisualBeat,
    VoiceSegment,
)

logger = get_logger(__name__)


class VisualBeatPlanningModule:
    """Cutting rhythm is read from the render profile
    (config/render_profiles.yaml), not hardcoded here: how fast a video
    changes shot is part of its output format, so a Shorts profile cuts
    faster than a long-form one without this module knowing either
    format exists. The profile is resolved lazily, at `plan()` time
    rather than construction, so building a `VideoAgent` never depends
    on the profiles file being readable.
    """

    def __init__(self, profile_name: str = DEFAULT_PROFILE_NAME) -> None:
        self._profile_name = profile_name

    def plan(
        self, segments: list[SegmentInput], voice_segments: list[VoiceSegment]
    ) -> list[VisualBeat]:
        profile = get_render_profile(self._profile_name)
        voice_by_segment = {voice.segment_id: voice for voice in voice_segments}
        beats: list[VisualBeat] = []
        for segment in sorted(segments, key=lambda s: s.order_index):
            voice_segment = voice_by_segment.get(segment.segment_id)
            if voice_segment is None:
                raise ValueError(
                    f"segment {segment.segment_id} (order {segment.order_index}) has no "
                    "voice segment — Visual Beat Planning sizes a segment's visuals from "
                    "its real narration duration, so it requires narration to already exist"
                )
            beats.extend(self._beats_for_segment(segment, voice_segment, profile))

        logger.info(
            "visual_beat_planning_complete",
            profile=profile.name,
            segment_count=len(segments),
            beat_count=len(beats),
            beats_per_segment=round(len(beats) / len(segments), 2) if segments else 0,
        )
        return beats

    def _beats_for_segment(
        self, segment: SegmentInput, voice_segment: VoiceSegment, profile: RenderProfile
    ) -> list[VisualBeat]:
        requirements = self._visual_requirements(segment)
        if not requirements:
            # Nothing provider-generated to show (e.g. a segment whose
            # only visual requirement is a render-time text overlay).
            # Rendering already handles that case; inventing a beat here
            # would be inventing a requirement the script never made.
            return []

        duration = max(voice_segment.duration_sec, 0.0)
        count = self._beat_count(duration, segment.production.pacing, profile)
        text_slices = self._split_narration(segment, voice_segment, count)

        beats: list[VisualBeat] = []
        for beat_index in range(count):
            requirement_index, requirement = requirements[beat_index % len(requirements)]
            start_sec = duration * beat_index / count
            end_sec = duration * (beat_index + 1) / count
            beats.append(
                VisualBeat(
                    segment_id=segment.segment_id,
                    order_index=segment.order_index,
                    beat_index=beat_index,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    narration_text=text_slices[beat_index],
                    requirement_index=requirement_index,
                    asset_type=requirement.asset_type,
                    requirement_description=requirement.description,
                )
            )
        return beats

    @staticmethod
    def _visual_requirements(segment: SegmentInput) -> list[tuple[int, AssetRequirement]]:
        """The segment's requirements that a beat can actually realize:
        visual (not sound effects/music cues) *and* provider-generated
        (not render-time overlays, which stay segment-wide, and not
        `subtitle_emphasis`, which isn't an asset at all — see
        `ASSET_TYPE_ROUTING`). Returns each with its original index so a
        beat can point back at the requirement it came from.
        """
        eligible: list[tuple[int, AssetRequirement]] = []
        for index, requirement in enumerate(segment.production.asset_requirements):
            if requirement.asset_type not in VISUAL_ASSET_TYPES:
                continue
            routing = ASSET_TYPE_ROUTING.get(requirement.asset_type)
            if routing is None:
                continue
            _, _, resolution_kind = routing
            if resolution_kind is not AssetResolutionKind.PROVIDER_GENERATED:
                continue
            eligible.append((index, requirement))
        return eligible

    @staticmethod
    def _beat_count(duration_sec: float, pacing: Pacing, profile: RenderProfile) -> int:
        """How many visuals this segment's narration should be split
        across — enough that no single visual overstays the profile's
        target for this pacing, but never so many that any of them falls
        below the profile's minimum on-screen time.
        """
        if duration_sec <= 0:
            return 1
        target = profile.seconds_per_visual(
            pacing.value if isinstance(pacing, Pacing) else str(pacing)
        )
        if target <= 0:
            return 1
        wanted = math.ceil(duration_sec / target)
        # Ceiling imposed by the minimum on-screen time: a 5s segment can
        # hold at most two 2.5s visuals however fast its pacing is.
        floor = max(profile.min_visual_duration_sec, 0.01)
        affordable = int(duration_sec // floor)
        return max(1, min(wanted, max(1, affordable)))

    @staticmethod
    def _split_narration(
        segment: SegmentInput, voice_segment: VoiceSegment, count: int
    ) -> list[str]:
        """Apportion the segment's narration across `count` beats.

        Uses the TTS provider's real per-word timings when it supplied
        them — assigning each word to whichever beat window it is
        actually spoken in — and falls back to an even split across words
        otherwise. That is the same real-timing-with-honest-fallback
        treatment Subtitle Generation already applies, for the same
        reason: not every provider reports word timing (Kokoro doesn't),
        and the fallback is good enough that requiring it would rule out
        working providers for no real gain.
        """
        words = segment.text.split()
        if not words:
            return [""] * count
        if count <= 1:
            return [segment.text]

        duration = max(voice_segment.duration_sec, 0.0)
        buckets: list[list[str]] = [[] for _ in range(count)]

        if voice_segment.word_timings and duration > 0:
            for timing in voice_segment.word_timings:
                midpoint = (timing.start_sec + timing.end_sec) / 2
                index = int(midpoint / duration * count) if duration else 0
                buckets[min(max(index, 0), count - 1)].append(timing.word)
        else:
            for position, word in enumerate(words):
                index = int(position / len(words) * count)
                buckets[min(index, count - 1)].append(word)

        # A beat whose window caught no words (possible with real timings
        # on a segment with long pauses) still needs something to build a
        # prompt from — fall back to the segment's full narration rather
        # than emitting an empty prompt.
        return [" ".join(bucket) if bucket else segment.text for bucket in buckets]
