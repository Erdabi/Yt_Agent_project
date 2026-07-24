"""Storyboard / Visual Planning Agent worker.

Registers the Celery task for the `storyboard` queue. The agent's actual
work — deciding, per script segment, the visual treatment (stock footage,
AI image, AI video, or text overlay) and resolving it to a concrete asset
— is not implemented yet. `run()` raises `NotImplementedError` rather
than fabricating a shot list; see
docs/architecture/03-agent-responsibilities.md §3.4 and
docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class StoryboardAgent(BaseAgent):
    name = "storyboard"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Storyboard agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.4."
        )


@celery_app.task(name="agents.storyboard.run", bind=True, base=AgentTask, queue="storyboard")
def run_storyboard_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return StoryboardAgent().execute_job(job_id)
