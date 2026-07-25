"""Thumbnail Generation module.

Runs fourth inside `VideoAgent.run()`: generates thumbnail candidates via
an image-gen provider and composites title text per the channel's style
guide. Has no real ordering dependency on assembly's output — it only
needs the approved script/title — but runs after it here to keep
`VideoAgent.run()` a single simple sequence rather than introducing
concurrency inside one job for a modest latency win. Not implemented yet
— see docs/architecture/03-agent-responsibilities.md §3.4.4 and
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
            "docs/architecture/03-agent-responsibilities.md §3.4.4."
        )
