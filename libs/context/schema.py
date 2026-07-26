"""The Project Context — everything a downstream agent needs about a
project, gathered once into a single immutable object instead of the
agent querying Channel/Project/VideoIdea/storage/settings itself.

Built by `build_project_context()` (builder.py). Every model here is
frozen (`ConfigDict(frozen=True)`): once built, a `ProjectContext` cannot
be mutated by whatever consumes it — an agent reads it, it never writes
it back. That matters because a `ProjectContext` may be passed through a
Celery task payload or held across a multi-step agent run; a consumer
accidentally mutating shared state would be a much harder bug to track
down than the `ValidationError` a frozen model raises immediately instead.

The Script Agent (services/agent_scriptwriter) is this object's first
real consumer — it receives one `ProjectContext` instead of separately
loading the channel, project, research fields, and knowledge package the
way services/agent_research/app/worker.py does.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from libs.models.enums import CompetitionLevel, ProjectStage, ProjectStatus
from libs.schemas.knowledge import KnowledgePackage

#: Bumped whenever a field is added/renamed/removed in a way a consumer
#: needs to branch on — same convention as
#: `libs.schemas.knowledge.CURRENT_SCHEMA_VERSION`.
CURRENT_SCHEMA_VERSION = "1.0"


class ChannelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    name: str
    niche: str | None = None
    persona: str | None = None
    banned_topics: list[str] = Field(default_factory=list)
    seed_topics: list[str] = Field(default_factory=list)
    youtube_channel_id: str | None = None


class ProjectMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str
    channel_id: str
    idea_id: str
    current_stage: ProjectStage
    status: ProjectStatus
    retry_count: int
    priority: int


class ResearchSummary(BaseModel):
    """The Research Agent's scored findings for this project's idea —
    distinct from the deep-research `KnowledgePackage` below, which is
    the source material the idea itself is grounded in.
    """

    model_config = ConfigDict(frozen=True)

    topic: str
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)
    rationale: str | None = None
    target_audience: str | None = None
    suggested_angle: str | None = None
    competition_level: CompetitionLevel | None = None
    suggested_length_sec: int | None = None
    research_notes: str | None = None


class ManagerSettings(BaseModel):
    """The subset of `libs.core.config.Settings` a downstream agent's
    behavior can legitimately depend on — not the full settings object,
    which also carries infrastructure config (DB/Redis credentials, etc.)
    no agent should need or be able to read through a `ProjectContext`.
    """

    model_config = ConfigDict(frozen=True)

    job_max_retries: int
    anthropic_model: str
    anthropic_effort: str


class ProjectContext(BaseModel):
    """The single immutable object `build_project_context()` returns. See
    module docstring.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = CURRENT_SCHEMA_VERSION
    built_at: datetime
    project: ProjectMetadata
    channel: ChannelProfile
    research: ResearchSummary
    #: `None` when the idea has no knowledge package yet — e.g. built
    #: before enrich mode ran, or a discover-mode idea never approved into
    #: a project. A consumer that requires one should check for `None`
    #: explicitly rather than assume it's always present.
    knowledge_package: KnowledgePackage | None = None
    #: The concrete resolved version (e.g. "v1") of the prompt template
    #: the requesting consumer will render — never the literal string
    #: "latest", even if that's what the underlying setting holds, so the
    #: usage-tracking/analytics record (libs/llm_usage) this version
    #: eventually feeds always shows what template was actually used.
    prompt_version: str
    manager: ManagerSettings
