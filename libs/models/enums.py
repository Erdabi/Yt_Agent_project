"""Enumerations shared between ORM models and agent code.

Plain `str` + `enum.Enum` so values serialize cleanly to JSON (Celery job
payloads, FastAPI responses) while still being stored as native Postgres
ENUM types via SQLAlchemy.
"""

import enum


class IdeaStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProjectStage(str, enum.Enum):
    """Mirrors the pipeline state machine in
    docs/architecture/01-system-architecture.md §1.3.
    """

    IDEATION = "ideation"
    SCRIPTING = "scripting"
    STORYBOARD = "storyboard"
    VOICEOVER = "voiceover"
    VIDEO_ASSEMBLY = "video_assembly"
    THUMBNAIL = "thumbnail"
    QA_REVIEW = "qa_review"
    AWAITING_APPROVAL = "awaiting_approval"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED_QA = "failed_qa"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    REJECTED = "rejected"
    ANALYTICS = "analytics"


class ProjectStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ScriptStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ShotType(str, enum.Enum):
    STOCK = "stock"
    AI_IMAGE = "ai_image"
    AI_VIDEO = "ai_video"
    TEXT_OVERLAY = "text_overlay"


class AssetType(str, enum.Enum):
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    MUSIC = "music"
    CAPTION = "caption"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"


class PublishStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class ProviderCapability(str, enum.Enum):
    LLM = "llm"
    TTS = "tts"
    IMAGE_GEN = "image_gen"
    VIDEO_GEN = "video_gen"
    STOCK_MEDIA = "stock_media"
