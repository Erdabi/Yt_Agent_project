"""Voice-over module.

Runs second inside `VideoAgent.run()`: sends each script segment to the
configured TTS provider, normalizes loudness, captures word-level
timestamps for caption sync. Not implemented yet — see
docs/architecture/03-agent-responsibilities.md §3.4.2 and
docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.schemas.jobs import JobContext


class VoiceoverModule:
    def synthesize(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Voice-over module logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4.2."
        )
