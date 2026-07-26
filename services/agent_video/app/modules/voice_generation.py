"""Voice Generation module.

Runs independently of Asset Planning/Asset Generation inside the Video
Agent's pipeline (see video_agent.py) — narration has no dependency on
which visual assets a segment resolves to. Sends each segment's text to
the configured `tts` provider and persists the result.

    Input:  list[SegmentInput]
    Output: list[VoiceSegment]

This is the *only* module that calls `libs.providers.get_provider("tts")`
— swapping ElevenLabs for Azure Speech in config/providers.yaml touches
nothing else in this pipeline. Every other module only ever sees the
`VoiceSegment` this one returns (an asset id, a storage path, a duration,
optional word timings) — never which vendor produced it.
"""

from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Voiceover
from libs.models.enums import AssetType as PhysicalAssetType
from libs.providers.registry import get_provider
from libs.storage import get_storage_backend

from ..pipeline_schema import SegmentInput, VoiceSegment

logger = get_logger(__name__)


class VoiceGenerationModule:
    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._provider = get_provider("tts")

    def synthesize(self, project_id: str, segments: list[SegmentInput]) -> list[VoiceSegment]:
        voice_segments = [self._synthesize_one(project_id, segment) for segment in segments]
        logger.info(
            "voice_generation_complete", project_id=project_id, segment_count=len(segments)
        )
        return voice_segments

    def _synthesize_one(self, project_id: str, segment: SegmentInput) -> VoiceSegment:
        provider_name = type(self._provider).__name__
        result = self._provider.synthesize(segment.text)

        storage_path = self._storage.save_bytes(
            project_id, "voiceover", f"{segment.segment_id}.mp3", result.audio_bytes
        )

        if result.word_timings:
            duration_sec = result.word_timings[-1].end_sec
        else:
            # No provider-supplied timing: fall back to the Script
            # Agent's own per-beat pacing estimate
            # (production_metadata.estimated_speech_wpm), the same figure
            # ScriptwriterAgent.run() already used for
            # ScriptSegment.estimated_duration_sec — reusing it here
            # keeps the two estimates consistent instead of computing
            # pacing two different ways.
            word_count = len(segment.text.split())
            duration_sec = (word_count / segment.production.estimated_speech_wpm) * 60

        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.AUDIO,
                provider=provider_name,
                storage_path=storage_path,
                duration_sec=duration_sec,
                metadata_={"segment_id": segment.segment_id},
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

            session.add(
                Voiceover(
                    script_segment_id=UUID(segment.segment_id),
                    asset_id=asset.id,
                    provider=provider_name,
                    duration_sec=duration_sec,
                )
            )

        return VoiceSegment(
            segment_id=segment.segment_id,
            order_index=segment.order_index,
            asset_id=asset_id,
            storage_path=storage_path,
            duration_sec=duration_sec,
            word_timings=result.word_timings,
            provider_name=provider_name,
        )
