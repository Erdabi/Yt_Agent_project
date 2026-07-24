"""The Orchestrator's FastAPI app.

This is the pipeline's coordination point (see
docs/architecture/01-system-architecture.md) — the only component
allowed to advance a project's stage or enforce approval gates. Today it
exposes real, read-only visibility into channels/projects/jobs plus a
real health check; the pipeline state machine and scheduler that actually
dispatch agent work land in Phase 1, once there is at least one agent with
real work to coordinate (see docs/architecture/06-roadmap.md) — standing
up that coordination logic against agents that only raise
`NotImplementedError` would be wiring with nothing real on the other end.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI

from libs.core.config import get_settings
from libs.core.logging import configure_logging, get_logger

from app.api import channels, health, jobs, projects

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
