"""Thumbnail Generation module.

Runs last inside `VideoAgent.run()`, alongside — not part of — the six
Asset Planning -> Rendering pipeline (see video_agent.py and
pipeline_schema.py): it has no real ordering dependency on that pipeline
at all, since it only needs the approved script/title and the channel's
style guide, not anything Rendering produces. It runs after it anyway to
keep `VideoAgent.run()` a single simple sequence rather than introducing
concurrency inside one job for a modest latency win. Not implemented yet
— see docs/architecture/03-agent-responsibilities.md §3.4 and
docs/architecture/06-roadmap.md, Phase 1. Its image-gen prompt
construction already exists at prompts/video/thumbnail_prompt/ (see
libs/prompts) — load it via
`get_prompt_loader().get("video", "thumbnail_prompt")` once this lands.
"""

from typing import Any

from libs.schemas.jobs import JobContext


class ThumbnailModule:
    def generate(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Thumbnail module logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4."
        )
