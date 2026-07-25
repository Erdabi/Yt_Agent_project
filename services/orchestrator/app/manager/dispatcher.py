"""Dispatch: how the Manager hands a stage to a worker agent.

Dispatch is two things done together, and both are durable:

1. A `jobs` row — the single source of truth for what the job was asked
   to do (`payload`) and, once the agent reports back, what happened.
2. A Celery message naming the agent's queue and task — carrying only the
   job id, never the payload itself (see libs/agents/base.py), so the DB
   row can't drift out of sync with what's actually running.

This is the send half of the "agent communication system"; the receive
half is `BaseAgent._notify_manager` (libs/agents/base.py) calling back
into `manager.advance_workflow` (tasks.py) when the agent is done.
"""

from uuid import UUID

from libs.core.celery_app import celery_app
from libs.core.db import sync_session_scope
from libs.models.enums import JobStatus
from libs.models.job import Job

from .workflow import WorkflowStep


def dispatch_step(project_id: str, step: WorkflowStep, payload: dict) -> str:
    """Create a `jobs` row for `step` against `project_id` and enqueue it
    on the agent's queue. Returns the new job id.
    """
    with sync_session_scope() as session:
        job = Job(
            project_id=UUID(project_id),
            agent_name=step.queue,
            queue_name=step.queue,
            status=JobStatus.QUEUED,
            payload=payload,
        )
        session.add(job)
        session.flush()
        job_id = str(job.id)

    celery_app.send_task(step.task_name, args=[job_id], queue=step.queue)
    return job_id
