"""Storyboard / Visual Planning module.

Runs first inside `VideoAgent.run()`: decides, per script segment, the
visual treatment (stock footage, AI image, AI video, or text overlay) and
resolves it to a concrete asset. Not implemented yet — see
docs/architecture/03-agent-responsibilities.md §3.4.1 and
docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.schemas.jobs import JobContext


class StoryboardModule:
    def plan_shots(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Storyboard module logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4.1."
        )
