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

    # --- Manager Agent: workflow retry policy ---------------------------
    # A worker agent's job is attempted exactly once per Celery task (see
    # libs/agents/base.py) — retrying a failed *stage* is a decision the
    # Manager Agent's reasoning engine makes (services/orchestrator/app/manager),
    # not something Celery does automatically. This bounds that decision:
    # once a project's Project.retry_count reaches this value, the Manager
    # escalates instead of retrying again, regardless of what the reasoning
    # engine's own judgment would otherwise choose.
    job_max_retries: int = Field(default=3, alias="JOB_MAX_RETRIES")

    # --- Error tracking ---------------------------------------------------
    # Left unset (None) in local development; every deployed environment
    # should set this. Left optional here rather than required so the
    # stack can boot locally without a Sentry account.
    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")

    # --- Manager Agent: Claude reasoning engine ---------------------------
    # Used by services/orchestrator/app/manager/reasoning.py to decide
    # advance/retry/escalate/abort at every stage transition. Left optional
    # so the stack still boots without a key — the Manager falls back to a
    # deterministic rule (see reasoning.py) rather than failing outright,
    # since a reasoning-engine outage must never be able to wedge the
    # pipeline.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-5", alias="ANTHROPIC_MODEL")
    anthropic_effort: str = Field(default="low", alias="ANTHROPIC_EFFORT")

    # --- Analytics Agent: independent scheduling -------------------------
    # How often the Analytics Agent's own Celery beat schedule sweeps
    # `PUBLISHED` projects (services/agent_analytics/app/worker.py). Not
    # read by the Manager at all — analytics runs decoupled from the
    # per-video pipeline (docs/architecture/01-system-architecture.md §1.5).
    analytics_sweep_interval_hours: int = Field(
        default=6, alias="ANALYTICS_SWEEP_INTERVAL_HOURS"
    )

    # --- Centralized asset storage (libs/storage) -------------------------
    # Every media artifact an agent produces is written here, keyed by
    # project id. Only "local" exists today; the interface (libs/storage/base.py)
    # is designed so a future S3/MinIO backend is a config change here, not
    # a rewrite of every agent that writes media.
    storage_backend: Literal["local"] = Field(default="local", alias="STORAGE_BACKEND")
    storage_root: str = Field(default="data", alias="STORAGE_ROOT")

    # --- Provider configuration system (libs/providers) --------------------
    # Path to the config file declaring which concrete provider class
    # backs each swappable capability (video generation, TTS, image
    # generation, YouTube) and its non-secret parameters — see
    # libs/providers/registry.py. Distinct from the `provider_configs` DB
    # table, which tracks a per-channel *active* choice among the names
    # this file defines.
    providers_config_path: str = Field(
        default="config/providers.yaml", alias="PROVIDERS_CONFIG_PATH"
    )

    # --- Prompt Management System (libs/prompts) ---------------------------
    # Root directory of versioned prompt template files
    # (prompts/<agent>/<name>/v<N>[.<provider>].yaml), loaded by
    # libs/prompts/registry.py rather than embedding prompt text as Python
    # string constants in agent code — see prompts/README.md.
    prompts_root: str = Field(default="prompts", alias="PROMPTS_ROOT")

    # --- Manager Agent: prompt template version ----------------------------
    # Which version of the manager/workflow_decision_* templates the
    # reasoning engine loads ("latest" resolves to the highest vN present
    # on disk). Pin this to roll back a prompt wording change without a
    # code change.
    manager_prompt_version: str = Field(default="latest", alias="MANAGER_PROMPT_VERSION")

    # --- Script Agent: prompt template version ------------------------------
    # Which version of prompts/script/generate_script/ the Project Context
    # Builder (libs/context) resolves for the Script Agent to use. Same
    # rationale as manager_prompt_version: pin it to roll back a wording
    # change without a code change.
    script_prompt_version: str = Field(default="latest", alias="SCRIPT_PROMPT_VERSION")

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
