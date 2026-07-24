"""Thumbnail Generation Agent worker.

Registers the Celery task for the `thumbnail` queue. The agent's actual
work — generating thumbnail candidates via an image-gen provider and
compositing title text per the channel's style guide — is not
implemented yet. `run()` raises `NotImplementedError` rather than
fabricating a thumbnail; see
docs/architecture/03-agent-responsibilities.md §3.7 and
docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class ThumbnailAgent(BaseAgent):
    name = "thumbnail"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Thumbnail agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.7."
        )


@celery_app.task(name="agents.thumbnail.run", bind=True, base=AgentTask, queue="thumbnail")
def run_thumbnail_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ThumbnailAgent().execute_job(job_id)
