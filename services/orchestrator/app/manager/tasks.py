"""The Manager's own Celery task — the receiving end of the agent
communication loop. Every agent, on finishing a job (success or failure),
sends `manager.advance_workflow` a job id (see
`BaseAgent._notify_manager` in libs/agents/base.py); this is where that
message lands and turns into a workflow decision.

Runs in its own worker process (the `manager_worker` Compose service,
consuming the `manager` queue) — a Celery worker counterpart to the
orchestrator's FastAPI process, both built from the same image and code.
"""

from libs.core.celery_app import AgentTask, celery_app

from .manager import ManagerAgent

_manager = ManagerAgent()


@celery_app.task(name="manager.advance_workflow", bind=True, base=AgentTask, queue="manager")
def advance_workflow_task(self: AgentTask, job_id: str) -> None:
    _manager.handle_job_finished(job_id)
