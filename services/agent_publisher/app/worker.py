"""Publisher Agent worker.

Registers the Celery task for the `publish` queue. The agent's actual
work — YouTube OAuth2 token management, the resumable upload via the
YouTube Data API v3, quota-aware pacing — is not implemented yet. `run()`
raises `NotImplementedError` rather than fabricating a publish result;
see docs/architecture/03-agent-responsibilities.md §3.9 and
docs/architecture/06-roadmap.md, Phase 1/3.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class PublisherAgent(BaseAgent):
    name = "publish"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Publisher agent logic lands in Phase 1/3 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.9."
        )


@celery_app.task(name="agents.publish.run", bind=True, base=AgentTask, queue="publish")
def run_publish_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return PublisherAgent().execute_job(job_id)
