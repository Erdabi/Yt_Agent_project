"""Pydantic response models for the orchestrator's HTTP API.

Kept separate from `libs/models` (the ORM layer) deliberately: these
describe what the API returns over the wire, not how a table is shaped —
the two are allowed to diverge (e.g. hiding an internal-only column)
without an API change forcing a migration, or vice versa.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from libs.models.enums import JobStatus, ProjectStage, ProjectStatus


class HealthOut(BaseModel):
    status: str
    database: bool
    redis: bool


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    niche: str | None
    is_active: bool
    created_at: datetime


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel_id: UUID
    idea_id: UUID
    current_stage: ProjectStage
    status: ProjectStatus
    retry_count: int
    priority: int
    created_at: datetime
    updated_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID | None
    agent_name: str
    queue_name: str
    status: JobStatus
    attempt_count: int
    max_attempts: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
