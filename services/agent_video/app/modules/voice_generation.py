"""Voice Generation module.

Runs independently of Asset Planning/Asset Generation inside the Video
Agent's pipeline (see video_agent.py) — narration has no dependency on
which visual assets a segment resolves to. Checks the Asset Cache
(../asset_cache.py) before sending each segment's text to the configured
`tts` provider, and persists the result either way.

    Input:  list[SegmentInput]
    Output: list[VoiceSegment]

This is the *only* module that calls `libs.providers.get_provider("tts")`
— swapping ElevenLabs for Azure Speech in config/providers.yaml touches
nothing else in this pipeline. Every other module only ever sees the
`VoiceSegment` this one returns (an asset id, a storage path, a duration,
optional word timings) — never which vendor produced it. The full
provider/model/voice id/language bundle each provider reports back
(`libs.providers.tts.base.SynthesisResult`) is persisted on `Voiceover`
and mirrored into `Asset.metadata_` (alongside the word timings
themselves) regardless of whether this segment was a cache hit or miss,
so it's durably queryable per project — not just cached transiently for
reuse.
"""

from uuid import UUID

from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Voiceover
from libs.models.enums import AssetType as PhysicalAssetType
from libs.providers.registry import get_provider
from libs.providers.tts.base import WordTiming

from ..asset_cache import AssetCache, AssetCacheKey
from ..pipeline_schema import SegmentInput, VoiceSegment

logger = get_logger(__name__)

#: Voice Generation's asset-type identity for the cache — narration isn't
#: one of the Script Agent's own `AssetType` values, so it gets its own
#: constant rather than borrowing an unrelated one.
_TTS_ASSET_TYPE = "narration"


class VoiceGenerationModule:
    def __init__(self) -> None:
        self._cache = AssetCache()
        self._provider = get_provider("tts")

    def synthesize(self, project_id: str, segments: list[SegmentInput]) -> list[VoiceSegment]:
        voice_segments = [self._synthesize_one(project_id, segment) for segment in segments]
        logger.info(
            "voice_generation_complete", project_id=project_id, segment_count=len(segments)
        )
        return voice_segments

    def _synthesize_one(self, project_id: str, segment: SegmentInput) -> VoiceSegment:
        provider_name = type(self._provider).__name__
        cache_key = AssetCacheKey(
            capability="tts",
            provider_name=provider_name,
            asset_type=_TTS_ASSET_TYPE,
            prompt=segment.text,
            # Settings the provider itself reports as output-affecting
            # (e.g. a configured voice_id/model) — folded in generically
            # so a config change doesn't wrongly reuse audio generated
            # under a different voice/model (see TTSProvider.cache_key_settings).
            settings=self._provider.cache_key_settings(),
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            storage_path = cached.storage_path
            provider_name = cached.provider_name
            duration_sec = cached.metadata["duration_sec"]
            word_timings = _word_timings_from_metadata(cached.metadata["word_timings"])
            model = cached.metadata.get("model")
            voice_id = cached.metadata.get("voice_id")
            language = cached.metadata.get("language")
        else:
            result = self._provider.synthesize(segment.text)

            if result.word_timings:
                duration_sec = result.word_timings[-1].end_sec
            else:
                # No provider-supplied timing: fall back to the Script
                # Agent's own per-beat pacing estimate
                # (production_metadata.estimated_speech_wpm), the same
                # figure ScriptwriterAgent.run() already used for
                # ScriptSegment.estimated_duration_sec — reusing it here
                # keeps the two estimates consistent instead of computing
                # pacing two different ways.
                word_count = len(segment.text.split())
                duration_sec = (word_count / segment.production.estimated_speech_wpm) * 60

            word_timings = result.word_timings
            model = result.model
            voice_id = result.voice_id
            language = result.language
            storage_path = self._cache.put(
                cache_key,
                data=result.audio_bytes,
                extension="mp3",
                physical_asset_type=PhysicalAssetType.AUDIO,
                provider_name=provider_name,
                metadata={
                    "duration_sec": duration_sec,
                    "word_timings": _word_timings_to_metadata(word_timings),
                    "model": model,
                    "voice_id": voice_id,
                    "language": language,
                },
            )

        with sync_session_scope() as session:
            asset = Asset(
                project_id=UUID(project_id),
                type=PhysicalAssetType.AUDIO,
                provider=provider_name,
                storage_path=storage_path,
                duration_sec=duration_sec,
                metadata_={
                    "segment_id": segment.segment_id,
                    "model": model,
                    "voice_id": voice_id,
                    "language": language,
                    "word_timings": _word_timings_to_metadata(word_timings),
                },
            )
            session.add(asset)
            session.flush()
            asset_id = str(asset.id)

            session.add(
                Voiceover(
                    script_segment_id=UUID(segment.segment_id),
                    asset_id=asset.id,
                    provider=provider_name,
                    voice_id=voice_id,
                    model=model,
                    language=language,
                    duration_sec=duration_sec,
                )
            )

        return VoiceSegment(
            segment_id=segment.segment_id,
            order_index=segment.order_index,
            asset_id=asset_id,
            storage_path=storage_path,
            duration_sec=duration_sec,
            word_timings=word_timings,
            provider_name=provider_name,
        )


def _word_timings_to_metadata(word_timings: list[WordTiming] | None) -> list[dict] | None:
    if word_timings is None:
        return None
    return [
        {"word": wt.word, "start_sec": wt.start_sec, "end_sec": wt.end_sec}
        for wt in word_timings
    ]


def _word_timings_from_metadata(raw: list[dict] | None) -> list[WordTiming] | None:
    if raw is None:
        return None
    return [
        WordTiming(word=item["word"], start_sec=item["start_sec"], end_sec=item["end_sec"])
        for item in raw
    ]
