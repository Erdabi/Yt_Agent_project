"""The agent execution framework.

Every concrete agent (one per `services/agent_*/app/worker.py`) subclasses
`BaseAgent` and implements `run()`. Everything else on this class — loading
the job, marking it running, recording success or failure, structured
logging — is real, working infrastructure that every future agent gets for
free. `run()` is deliberately the only method a subclass needs to write;
it's where an agent's actual work (calling an LLM, a TTS provider, ffmpeg,
the YouTube API) will live once implemented.

`execute_job` is the only entry point. Celery task wrappers
(`services/agent_*/app/worker.py`) call it; nothing else should.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from libs.core.celery_app import celery_app
from libs.core.db import sync_session_scope
from libs.core.logging import bind_job_context, clear_job_context, get_logger
from libs.models.enums import JobStatus
from libs.models.job import Job
from libs.schemas.jobs import JobContext

logger = get_logger(__name__)


class BaseAgent(ABC):
    """Base class for every agent. Subclasses set `name` and implement `run`."""

    #: Short, stable identifier (e.g. "research", "scriptwriter"). Used in
    #: log lines and must match the `agent_name` a Job row is created with.
    name: str

    @abstractmethod
    def run(self, context: JobContext) -> dict[str, Any]:
        """Do the agent's actual work and return a JSON-serializable result.

        Must raise on failure. Returning an error sentinel instead of
        raising would hide the failure from both the Manager Agent's
        retry/escalate decision and the `jobs.error` audit trail.
        """

    def execute_job(self, job_id: str) -> dict[str, Any]:
        """Load `job_id`, run it, and record the outcome — success or
        failure — back onto the same row. Every call reaches a terminal
        status (`SUCCEEDED` or `FAILED`) and then notifies the Manager
        Agent, whether `run()` succeeded or raised.
        """
        context, attempt_count = self._mark_running(job_id)

        bind_job_context(
            job_id=context.job_id,
            project_id=context.project_id,
            agent_name=self.name,
            attempt=attempt_count,
        )
        logger.info("agent_job_started")

        try:
            result = self.run(context)
        except Exception as exc:
            logger.error("agent_job_failed", error=str(exc))
            self._mark_finished(job_id, error=str(exc))
            raise
        else:
            logger.info("agent_job_succeeded")
            self._mark_finished(job_id, result=result)
            return result
        finally:
            self._notify_manager(job_id)
            clear_job_context()

    def _mark_running(self, job_id: str) -> tuple[JobContext, int]:
        with sync_session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise LookupError(f"job {job_id} not found")

            job.status = JobStatus.RUNNING
            job.attempt_count += 1
            job.started_at = datetime.now(timezone.utc)
            session.flush()

            context = JobContext(
                job_id=str(job.id),
                project_id=str(job.project_id) if job.project_id else None,
                agent_name=job.agent_name,
                payload=job.payload,
            )
            return context, job.attempt_count

    def _mark_finished(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with sync_session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                # The job row was deleted out from under us; nothing to record.
                return

            job.finished_at = datetime.now(timezone.utc)
            if error is None:
                job.status = JobStatus.SUCCEEDED
                job.result = result or {}
            else:
                job.error = error
                job.status = JobStatus.FAILED

    def _notify_manager(self, job_id: str) -> None:
        """Tell the Manager Agent this job has reached a terminal state, so
        it can decide the next step (advance/retry/escalate/abort).

        Sent by task *name* on the shared broker — not a Python import of
        the orchestrator's code — so an agent never depends on the
        Manager's implementation, only on its queue name and task name.
        This is the whole "agent communication system": agents don't call
        the Manager, they report to it through the same broker it already
        uses to dispatch them.

        Never allowed to raise: a transient broker hiccup here must not
        mask the real result `execute_job` already recorded above.
        """
        try:
            celery_app.send_task("manager.advance_workflow", args=[job_id], queue="manager")
        except Exception as exc:
            logger.error("manager_notification_failed", error=str(exc))
