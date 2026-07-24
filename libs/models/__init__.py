"""Import every model module so `Base.metadata` is fully populated and
SQLAlchemy can resolve the string-based `relationship()` references between
them. `migrations/env.py` imports `Base` from here (via `libs.models.base`)
after importing this package.
"""

from libs.models.asset import Asset, Render, Thumbnail, Voiceover
from libs.models.base import Base
from libs.models.channel import Channel
from libs.models.idea import VideoIdea
from libs.models.job import Job
from libs.models.project import Project
from libs.models.provider import ProviderConfig, ProviderUsageLog
from libs.models.publication import PerformanceMetric, Publication
from libs.models.qa import QAReport
from libs.models.script import Script, ScriptSegment
from libs.models.storyboard import StoryboardShot
from libs.models.system import SystemEvent
from libs.models.user import User

__all__ = [
    "Base",
    "Channel",
    "VideoIdea",
    "Project",
    "Script",
    "ScriptSegment",
    "StoryboardShot",
    "Asset",
    "Voiceover",
    "Render",
    "Thumbnail",
    "QAReport",
    "Job",
    "Publication",
    "PerformanceMetric",
    "ProviderConfig",
    "ProviderUsageLog",
    "SystemEvent",
    "User",
]
