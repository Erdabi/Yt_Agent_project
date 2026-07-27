"""Quality Control Agent worker.

Registers the Celery task for the `qa` queue — the Manager dispatches
exactly one job here for the whole `quality_check` stage. See
quality_control_agent.py for the reviewer-coordination design.
"""

from typing import Any

from libs.core.celery_app import AgentTask, celery_app

from .quality_control_agent import QualityControlAgent


@celery_app.task(name="agents.qa.run", bind=True, base=AgentTask, queue="qa")
def run_qa_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return QualityControlAgent().execute_job(job_id)
