"""The Orchestrator's FastAPI app.

This is the pipeline's coordination point (see
docs/architecture/01-system-architecture.md) — the only component
allowed to advance a project's stage or enforce approval gates. It exposes
`POST /goals` (the Manager Agent's entry point — see app/manager) plus
read-only visibility into channels/projects/jobs and a real health check.
The Manager's own decision loop runs in a separate Celery worker process
(the `manager_worker` Compose service, consuming the `manager` queue via
app/manager/tasks.py) rather than in this FastAPI process.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI

from libs.core.config import get_settings
from libs.core.logging import configure_logging, get_logger

from app.api import channels, goals, health, jobs, projects

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings)
    logger = get_logger(__name__)

    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)

    logger.info("orchestrator_starting", environment=settings.environment)
    yield
    logger.info("orchestrator_stopping")


app = FastAPI(title="Yt Agent Orchestrator", lifespan=lifespan)

app.include_router(health.router)
app.include_router(channels.router)
app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(goals.router)
