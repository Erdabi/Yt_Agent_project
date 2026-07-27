"""Publisher Agent worker.

Registers the Celery task for the `publish` queue — the Manager
dispatches exactly one job here for the whole `publishing` stage. See
publisher_agent.py for the upload/verify/persist design.
"""

from typing import Any

from libs.core.celery_app import AgentTask, celery_app

from .publisher_agent import PublisherAgent


@celery_app.task(name="agents.publish.run", bind=True, base=AgentTask, queue="publish")
def run_publish_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return PublisherAgent().execute_job(job_id)
