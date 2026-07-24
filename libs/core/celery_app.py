"""Celery application factory + the base Task class every agent job uses.

A concrete agent's `worker.py` is a thin wrapper: it imports `celery_app`,
registers one task per queue with `base=AgentTask`, and that task's body is
one line — `return SomeAgent().execute_job(job_id)`. Retry policy (how many
attempts, how long to back off) is centralized on `AgentTask` here so every
agent behaves consistently unless a specific task overrides it.
"""

from celery import Celery, Task
from celery.signals import setup_logging

from libs.core.config import get_settings
from libs.core.logging import configure_logging, get_logger

settings = get_settings()


def create_celery_app() -> Celery:
    app = Celery(
        "yt_agent",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend_url,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_expires=60 * 60 * 24 * 7,  # 7 days — enough to debug a stuck job
        # Only ack a task after it completes (success or final failure),
        # so a worker crash mid-job redelivers it instead of silently
        # dropping it.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        broker_connection_retry_on_startup=True,
        # One task at a time per worker process — the video assembly agent
        # in particular is CPU-bound; prefetching more just skews queue
        # wait-time reporting without a throughput benefit.
        worker_prefetch_multiplier=1,
    )
    return app


celery_app = create_celery_app()


@setup_logging.connect
def _configure_worker_logging(**kwargs: object) -> None:
    """Celery installs its own logging config on worker boot by default;
    this replaces it with ours so worker logs have the same structured
    shape as every other service (see libs/core/logging.py).
    """
    configure_logging(settings)


class AgentTask(Task):
    """Shared base Task for every agent job.

    `autoretry_for`/`retry_backoff*` are read by Celery from this class
    when a task is registered with `base=AgentTask` — see
    https://docs.celeryq.dev/en/stable/userguide/tasks.html#automatic-retry-for-known-exceptions.
    """

    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = settings.job_retry_backoff_seconds * 10
    retry_jitter = True
    max_retries = settings.job_max_retries

    def on_failure(self, exc: BaseException, task_id: str, args: tuple, kwargs: dict, einfo: object) -> None:
        get_logger(__name__).error(
            "celery_task_failed_terminally",
            task_id=task_id,
            task_name=self.name,
            error=str(exc),
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)
