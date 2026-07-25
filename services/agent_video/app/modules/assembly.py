"""Video Assembly module.

Runs third inside `VideoAgent.run()`, after storyboard and voiceover have
both produced their output: composites voice-over audio, visuals, music,
and captions into the final render via ffmpeg. Not implemented yet — see
docs/architecture/03-agent-responsibilities.md §3.4.3 and
docs/architecture/06-roadmap.md, Phase 1. (ffmpeg and any Python
compositing library are deliberately not installed in this image yet —
see services/agent_video/requirements.txt — since nothing here uses them
until that implementation lands.)
"""

from typing import Any

from libs.schemas.jobs import JobContext


class AssemblyModule:
    def render(
        self,
        context: JobContext,
        storyboard_result: dict[str, Any],
        voiceover_result: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "Video Assembly module logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4.3."
        )
