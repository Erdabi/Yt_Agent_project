"""The contract between the Orchestrator and every agent.

Only a `job_id` (a UUID string) ever crosses the Celery broker — see
`libs/core/celery_app.py`. Everything an agent actually needs (its
project, its payload) lives in the `jobs` row in Postgres and is loaded
from there. `JobContext` is the typed shape that loading produces; every
concrete agent's `run()` method receives one.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class JobContext(BaseModel):
    """What `BaseAgent.execute_job` loads from a `jobs` row and hands to
    a concrete agent's `run()` method. Immutable — an agent has no
    business mutating its own inputs.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    project_id: str | None
    agent_name: str
    payload: dict[str, Any]
