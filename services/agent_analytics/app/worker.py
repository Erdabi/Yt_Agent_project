"""Analytics / Performance Tracking Agent worker.

Deliberately outside the Manager's pipeline. Every other agent in this
codebase is dispatched by the Manager as one step in its fixed workflow
(services/orchestrator/app/manager/workflow.py) and reports back into its
advance/retry/escalate decision loop. Analytics does neither: a project
reaching `PUBLISHED` is where the Manager's involvement ends, not a
trigger for more Manager-tracked work — see
docs/architecture/01-system-architecture.md §1.5, step 9. Instead:

- `sweep_published_projects` runs on its own recurring schedule (Celery
  beat, `beat_schedule` below — no Manager involved) and dispatches one
  analytics job per published project.
- `AnalyticsAgent.reports_to_manager = False`, so finishing a job never
  calls back into `manager.advance_workflow` (see
  `BaseAgent.execute_job`/`_notify_manager` in libs/agents/base.py) — a
  failed or slow metrics pull must never be able to affect a video's
  production status or retry count.

The actual work of pulling views/watch-time/CTR/etc. from the YouTube
Analytics API is still not implemented; `AnalyticsAgent.run` raises
`NotImplementedError` rather than fabricating performance numbers — see
docs/architecture/03-agent-responsibilities.md §3.10 and
docs/architecture/06-roadmap.md, Phase 3. `sweep_published_projects`
itself, by contrast, is real: "schedule Analytics independently of the
pipeline" is infrastructure this turn is responsible for, distinct from
the YouTube Analytics API integration that isn't.
"""

from typing import Any

from celery.schedules import crontab
from sqlalchemy import select

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.core.config import get_settings
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.enums import JobStatus, PublishStatus
from libs.models.job import Job
from libs.models.publication import Publication
from libs.schemas.jobs import JobContext

logger = get_logger(__name__)


class AnalyticsAgent(BaseAgent):
    name = "analytics"
    reports_to_manager = False

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Analytics agent logic lands in Phase 3 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.10."
        )


@celery_app.task(name="agents.analytics.run", bind=True, base=AgentTask, queue="analytics")
def run_analytics_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return AnalyticsAgent().execute_job(job_id)


@celery_app.task(name="agents.analytics.sweep", bind=True, base=AgentTask, queue="analytics")
def sweep_published_projects(self: AgentTask) -> int:
    """Find every published project and dispatch one analytics job per
    publication. Fired on the recurring schedule below (`beat_schedule`),
    never by the Manager.

    Creates a `jobs` row per publication for the same reason every other
    agent invocation does — audit trail, status tracking, error capture
    (docs/architecture/04-database-design.md) — but dispatches the actual
    task directly rather than through the orchestrator's
    `manager.dispatcher.dispatch_step`: that helper is keyed to a
    `WorkflowStep` from the Manager's fixed plan, and an Analytics sweep
    is deliberately not a step in that plan.
    """
    with sync_session_scope() as session:
        publications = session.scalars(
            select(Publication).where(Publication.publish_status == PublishStatus.PUBLISHED)
        ).all()

        job_ids: list[str] = []
        for publication in publications:
            job = Job(
                project_id=publication.project_id,
                agent_name=AnalyticsAgent.name,
                queue_name="analytics",
                status=JobStatus.QUEUED,
                payload={"publication_id": str(publication.id)},
            )
            session.add(job)
            session.flush()
            job_ids.append(str(job.id))

    for job_id in job_ids:
        celery_app.send_task("agents.analytics.run", args=[job_id], queue="analytics")

    logger.info("analytics_sweep_dispatched", job_count=len(job_ids))
    return len(job_ids)


# Registered on the shared `celery_app` singleton, but only processes that
# import *this* module (i.e. this service's own worker/beat containers,
# both started with `-A app.worker:celery_app`) ever see it — every other
# service's process imports its own `app.worker` instead, so this can't
# leak a schedule into the orchestrator or another agent's process.
celery_app.conf.beat_schedule = {
    "analytics-sweep": {
        "task": "agents.analytics.sweep",
        "schedule": crontab(minute=0, hour=f"*/{get_settings().analytics_sweep_interval_hours}"),
        "options": {"queue": "analytics"},
    },
}
