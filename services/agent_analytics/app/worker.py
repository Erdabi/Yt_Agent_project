"""Analytics / Performance Tracking Agent worker.

Registers the Celery task for the `analytics` queue. The agent's actual
work — pulling views/watch-time/CTR/etc. from the YouTube Analytics API
for published projects — is not implemented yet. `run()` raises
`NotImplementedError` rather than fabricating performance numbers; see
docs/architecture/03-agent-responsibilities.md §3.10 and
docs/architecture/06-roadmap.md, Phase 3.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class AnalyticsAgent(BaseAgent):
    name = "analytics"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Analytics agent logic lands in Phase 3 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.10."
        )


@celery_app.task(name="agents.analytics.run", bind=True, base=AgentTask, queue="analytics")
def run_analytics_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return AnalyticsAgent().execute_job(job_id)
