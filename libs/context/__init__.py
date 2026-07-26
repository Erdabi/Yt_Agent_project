"""Project Context Builder.

`build_project_context(project_id)` gathers the Channel Profile, Project
metadata, Research summary, Knowledge Package, resolved prompt version,
and Manager settings for one project into a single immutable
`ProjectContext` — see schema.py and builder.py.

Usage:

    from libs.context import build_project_context

    context = build_project_context(project_id)
    # context.channel, context.project, context.research,
    # context.knowledge_package, context.prompt_version, context.manager
"""

from .builder import build_project_context
from .schema import ChannelProfile, ManagerSettings, ProjectContext, ProjectMetadata, ResearchSummary

__all__ = [
    "build_project_context",
    "ProjectContext",
    "ChannelProfile",
    "ProjectMetadata",
    "ResearchSummary",
    "ManagerSettings",
]
