"""Builds one `ProjectContext` per call: gather-once, read-many.

`build_project_context()` is the *only* place that reads Channel/Project/
VideoIdea rows and the stored Knowledge Package to answer "what does an
agent need to know about this project" — a consumer calls this once and
gets back an immutable snapshot, instead of separately querying the
database and storage itself the way services/agent_research/app/worker.py
does today. It never writes anything; building a context is a pure read.
"""

from datetime import UTC, datetime
from uuid import UUID

from libs.core.config import get_settings
from libs.core.db import sync_session_scope
from libs.models.channel import Channel
from libs.models.idea import VideoIdea
from libs.models.project import Project
from libs.prompts import get_prompt_loader
from libs.schemas.knowledge import KnowledgePackage
from libs.storage import get_storage_backend

from .schema import (
    ChannelProfile,
    ManagerSettings,
    ProjectContext,
    ProjectMetadata,
    ResearchSummary,
)


def build_project_context(
    project_id: str,
    *,
    # (agent, prompt name) the *caller* is about to render a prompt for —
    # used only to resolve `ProjectContext.prompt_version` to the concrete
    # version that agent's `<agent>_prompt_version` setting currently
    # points at. Defaults to the Script Agent's system-prompt slot since
    # it's this object's first real consumer (see schema.py's module
    # docstring and services/agent_scriptwriter/app/script_generator.py).
    consumer_prompt: tuple[str, str] = ("script", "generate_script_system"),
) -> ProjectContext:
    with sync_session_scope() as session:
        project = session.get(Project, UUID(project_id))
        if project is None:
            raise LookupError(f"project {project_id} not found")
        idea = session.get(VideoIdea, project.idea_id)
        if idea is None:
            raise LookupError(f"idea {project.idea_id} not found")
        channel = session.get(Channel, project.channel_id)
        if channel is None:
            raise LookupError(f"channel {project.channel_id} not found")

        project_metadata = ProjectMetadata(
            project_id=str(project.id),
            channel_id=str(project.channel_id),
            idea_id=str(project.idea_id),
            current_stage=project.current_stage,
            status=project.status,
            retry_count=project.retry_count,
            priority=project.priority,
        )

        persona = channel.persona_config or {}
        channel_profile = ChannelProfile(
            channel_id=str(channel.id),
            name=channel.name,
            niche=channel.niche,
            persona=persona.get("tone") or persona.get("persona"),
            banned_topics=list(persona.get("banned_topics") or []),
            seed_topics=list(persona.get("seed_topics") or []),
            youtube_channel_id=channel.youtube_channel_id,
            style_guide_summary=persona.get("style_guide_summary"),
        )

        research_summary = ResearchSummary(
            topic=idea.title,
            description=idea.description,
            keywords=list(idea.keywords or []),
            rationale=idea.rationale,
            target_audience=idea.target_audience,
            suggested_angle=idea.suggested_angle,
            competition_level=idea.competition_level,
            suggested_length_sec=idea.suggested_length_sec,
            research_notes=idea.research_notes,
        )

        # Captured now, read from storage after the session closes —
        # storage reads don't need (and shouldn't hold open) a DB
        # transaction.
        knowledge_package_path = idea.knowledge_package_json_path

    knowledge_package: KnowledgePackage | None = None
    if knowledge_package_path:
        storage = get_storage_backend()
        knowledge_package = KnowledgePackage.model_validate_json(
            storage.read_bytes(knowledge_package_path)
        )

    settings = get_settings()
    prompt_agent, prompt_name = consumer_prompt
    # Convention: a per-agent prompt version setting is named
    # "<agent>_prompt_version" (e.g. manager_prompt_version,
    # script_prompt_version — libs/core/config.py). An agent without one
    # yet falls back to "latest" rather than raising, since a Script
    # Agent not having a dedicated setting is not this builder's problem
    # to enforce.
    prompt_version_setting = getattr(settings, f"{prompt_agent}_prompt_version", "latest")
    resolved_prompt_version = get_prompt_loader().get(
        prompt_agent, prompt_name, version=prompt_version_setting
    ).version

    manager_settings = ManagerSettings(
        job_max_retries=settings.job_max_retries,
        anthropic_model=settings.anthropic_model,
        anthropic_effort=settings.anthropic_effort,
    )

    return ProjectContext(
        built_at=datetime.now(UTC),
        project=project_metadata,
        channel=channel_profile,
        research=research_summary,
        knowledge_package=knowledge_package,
        prompt_version=resolved_prompt_version,
        manager=manager_settings,
    )
