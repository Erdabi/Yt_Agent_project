"""Structured logging, shared by every service.

Every service calls `configure_logging(get_settings())` exactly once, as the
first thing it does on startup (FastAPI lifespan / Celery worker init
signal). After that, `get_logger(__name__)` produces structured log events
that are:

- Tagged with `service` and `environment` on every line, so a mixed log
  stream (e.g. `docker compose logs`) is attributable at a glance.
- Rendered as JSON in staging/production (ready to ship to a log
  aggregator later without any code changes) and as readable colored text
  in local development.
- The same format whether the log line came from our code or from a
  library using the stdlib `logging` module (uvicorn, SQLAlchemy, Celery) —
  both are routed through the same formatter, so there is one log shape to
  read, not two.

`bind_job_context` / `clear_job_context` let the agent framework
(libs/agents/base.py) attach job_id/project_id/stage/attempt to every log
line for the duration of a job, without threading those values through
every function call by hand.
"""

import logging
import sys
from collections.abc import Mapping
from typing import Any

import structlog

from libs.core.config import Settings

_SHARED_PROCESSORS: list[structlog.types.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route stdlib `logging` through the same pipeline."""

    def add_service_metadata(
        logger: object, method_name: str, event_dict: dict[str, Any]
    ) -> Mapping[str, Any]:
        event_dict.setdefault("service", settings.service_name)
        event_dict.setdefault("environment", settings.environment)
        return event_dict

    shared_processors = [*_SHARED_PROCESSORS, add_service_metadata]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if settings.environment == "local"
        else structlog.processors.JSONRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.log_level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger, conventionally called with `__name__`."""
    return structlog.get_logger(name)


def bind_job_context(**kwargs: Any) -> None:
    """Attach fields (e.g. job_id, project_id, stage, attempt) to every log
    line emitted on this task/request until `clear_job_context()` is called.
    """
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_job_context() -> None:
    structlog.contextvars.clear_contextvars()
