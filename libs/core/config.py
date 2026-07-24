"""Typed application configuration.

Every environment variable the system reads is declared here, once, as a
Pydantic field. That gives two properties every other module in this
codebase relies on:

1. Startup fails immediately with a readable error if a required variable
   is missing or malformed, instead of failing confusingly later (e.g. deep
   inside a database call).
2. This module is the single, greppable source of truth for what the
   system can be configured to do — nothing reads `os.environ` directly
   anywhere else in `libs/` or `services/`.

Every service (orchestrator, each agent worker) imports `get_settings()`
from here rather than defining its own config handling.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity -----------------------------------------------------
    # SERVICE_NAME is set per-container in docker-compose.yml (e.g.
    # "orchestrator", "agent_research") and flows into every log line so
    # log output is attributable to a specific service without relying on
    # the container hostname.
    service_name: str = Field(default="app", alias="SERVICE_NAME")
    environment: Literal["local", "staging", "production"] = Field(
        default="local", alias="APP_ENV"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- PostgreSQL -----------------------------------------------------
    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(alias="POSTGRES_DB")
    postgres_user: str = Field(alias="POSTGRES_USER")
    postgres_password: str = Field(alias="POSTGRES_PASSWORD")
    database_pool_size: int = Field(default=5, alias="DATABASE_POOL_SIZE")

    # --- Redis (Celery broker/result backend + rate-limit locks) -------
    redis_host: str = Field(default="redis", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_broker_db: int = Field(default=0, alias="REDIS_BROKER_DB")
    redis_result_backend_db: int = Field(default=1, alias="REDIS_RESULT_BACKEND_DB")

    # --- Job/agent execution policy ------------------------------------
    # Shared defaults for the agent framework's retry behavior (libs/agents/base.py).
    # An individual agent task can still override these per-call.
    job_max_retries: int = Field(default=3, alias="JOB_MAX_RETRIES")
    job_retry_backoff_seconds: int = Field(default=10, alias="JOB_RETRY_BACKOFF_SECONDS")

    # --- Error tracking ---------------------------------------------------
    # Left unset (None) in local development; every deployed environment
    # should set this. Left optional here rather than required so the
    # stack can boot locally without a Sentry account.
    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL (asyncpg driver).

        Used by the orchestrator's FastAPI app, whose request handlers are
        async. Agent workers do not use this — see `sync_database_url`.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[misc]
    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy URL (psycopg driver).

        Used by Celery agent workers and Alembic. Celery's task model is
        synchronous (prefork worker processes); driving async DB calls from
        inside a sync task adds an event-loop-per-task dance for no benefit
        here, so agent workers get a plain synchronous engine instead.
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[misc]
    @property
    def celery_broker_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_broker_db}"

    @computed_field  # type: ignore[misc]
    @property
    def celery_result_backend_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_result_backend_db}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings instance.

    `lru_cache` means the environment is parsed and validated exactly once
    per process (not per request/task), and every caller gets the same
    instance.
    """
    return Settings()
