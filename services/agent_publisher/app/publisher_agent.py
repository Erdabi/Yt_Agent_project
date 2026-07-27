"""The Publisher Agent.

Publishes a project's finished, QA-approved video to YouTube: refines its
title/description/tags/playlist via `MetadataGenerator`, uploads the video
and thumbnail through the configured `youtube` provider
(`libs.providers.get_provider("youtube")`), verifies the upload actually
succeeded, and persists everything onto that project's `Publication` row.

    Input:  JobContext (project_id, optional payload overrides)
    Output: dict (publication id, video id, url, upload status, playlist)

Architecture — reuse, not reinvention:
- **Provider Registry** (`libs.providers.get_provider("youtube")`) — every
  YouTube Data API call goes through the configured `YouTubeProvider`,
  never a hardcoded vendor SDK/class — see
  libs/providers/youtube/base.py's own docstring for the four operations
  it exposes and why.
- **Project Context** (`libs.context.build_project_context`) — supplies
  the channel profile and research summary the same way the Quality
  Control Agent already does; script/render/thumbnail production data
  isn't part of `ProjectContext` (predates it), so `_gather` reads those
  directly, the same scoping decision QualityControlAgent._gather makes.
- **MetadataGenerator** (metadata_generator.py) — the only LLM call this
  agent makes, routed through the `llm` Provider Registry capability.

Idempotency: every project has at most one `Publication` row. If a prior,
partially-completed attempt already recorded a `youtube_video_id`, this
run skips the upload entirely and resumes from the thumbnail/playlist/
verify steps against that same video — a retried job upserts, it never
re-uploads (docs/architecture/03-agent-responsibilities.md §3.9). The
playlist addition is persisted the moment it succeeds (not just at the
end), so a retry after a later step fails also skips a redundant
`add_to_playlist` call — the one gap this doesn't cover is this process
crashing in the narrow window between that API call succeeding and the
following DB write, which no in-process retry logic can close without a
real distributed transaction; that residual case is an accepted,
documented tradeoff, not a fixable bug.

Never marks a project published without first calling `verify_upload`:
the whole point of that call is to be YouTube's own confirmation, not
`upload_video`'s self-reported result. An unhealthy `upload_status`
("failed"/"rejected") fails the job just like any other provider error.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.agents.base import BaseAgent
from libs.context import ProjectContext, build_project_context
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Render, Thumbnail
from libs.models.channel import Channel
from libs.models.enums import PublishStatus
from libs.models.publication import Publication
from libs.models.script import Script
from libs.providers.registry import get_provider
from libs.providers.youtube.base import (
    UploadResult,
    VideoMetadata,
    YouTubeProvider,
    YouTubeProviderError,
    YouTubeUploadError,
)
from libs.schemas.jobs import JobContext
from libs.storage import get_storage_backend

from .metadata_generator import MetadataGenerator

logger = get_logger(__name__)

#: YouTube's own `status.uploadStatus` values that mean the upload must
#: never be treated as a success, however far the rest of this job got.
_UNHEALTHY_UPLOAD_STATUSES = frozenset({"failed", "rejected"})


@dataclass(frozen=True)
class _Gathered:
    project_context: ProjectContext
    script_content: str
    video_bytes: bytes
    thumbnail_bytes: bytes
    channel_persona_config: dict
    existing_video_id: str | None
    existing_playlist_id: str | None


class PublisherAgent(BaseAgent):
    name = "publish"

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Publisher Agent requires a project_id — it is always "
                "dispatched against an existing project"
            )
        project_id = context.project_id
        payload = context.payload or {}
        dry_run = payload.get("dry_run")

        gathered = self._gather(project_id)
        metadata = self._build_metadata(gathered, payload)
        playlist_id = metadata.playlist_id
        playlist_already_added = bool(playlist_id) and playlist_id == gathered.existing_playlist_id

        provider: YouTubeProvider = get_provider("youtube")
        video_id = gathered.existing_video_id

        try:
            if video_id is None:
                upload_result = provider.upload_video(gathered.video_bytes, metadata, dry_run=dry_run)
                video_id = upload_result.video_id
                self._persist_after_upload(project_id, upload_result, metadata)
            else:
                logger.info("publisher_resuming_existing_upload", project_id=project_id, video_id=video_id)

            provider.set_thumbnail(video_id, gathered.thumbnail_bytes, dry_run=dry_run)

            playlist_added = playlist_already_added
            if playlist_id and not playlist_already_added:
                provider.add_to_playlist(video_id, playlist_id, dry_run=dry_run)
                playlist_added = True
                # Persisted immediately (not just at the end, alongside
                # everything else in _persist_success) so a subsequent
                # verify_upload failure can't lose the fact that this
                # step already succeeded — without this, a retry would
                # have no record of it and would add the video to the
                # playlist a second time.
                self._persist_playlist_added(project_id, playlist_id)

            verified = provider.verify_upload(video_id, dry_run=dry_run)
        except YouTubeProviderError as exc:
            logger.error("publisher_job_failed", project_id=project_id, video_id=video_id, error=str(exc))
            self._persist_failure(project_id, video_id, metadata, error=str(exc))
            raise

        if verified.upload_status in _UNHEALTHY_UPLOAD_STATUSES:
            error = f"YouTube reports an unhealthy upload status for {video_id!r}: {verified.upload_status!r}"
            self._persist_failure(project_id, video_id, metadata, error=error)
            raise YouTubeUploadError(error)

        publication_id = self._persist_success(
            project_id, verified, metadata, playlist_id if playlist_added else None
        )

        logger.info(
            "publisher_complete",
            project_id=project_id,
            video_id=verified.video_id,
            upload_status=verified.upload_status,
            scheduled=bool(metadata.publish_at),
            dry_run=bool(dry_run),
        )
        return {
            "publication_id": publication_id,
            "youtube_video_id": verified.video_id,
            "url": verified.url,
            "upload_status": verified.upload_status,
            "privacy_status": verified.privacy_status,
            "playlist_id": playlist_id if playlist_added else None,
        }

    # --- Gathering: everything the metadata refinement + upload need -------

    def _gather(self, project_id: str) -> _Gathered:
        project_context = build_project_context(
            project_id, consumer_prompt=("publish", "refine_metadata_system")
        )

        with sync_session_scope() as session:
            channel = session.get(Channel, UUID(project_context.channel.channel_id))
            persona_config = (channel.persona_config or {}) if channel else {}

            script = session.scalar(
                select(Script)
                .where(Script.project_id == UUID(project_id))
                .order_by(Script.version.desc())
                .limit(1)
            )
            if script is None:
                raise LookupError(f"no script found for project {project_id}")
            script_content = script.content

            render_row = session.scalar(
                select(Render)
                .join(Asset, Render.asset_id == Asset.id)
                .where(Render.project_id == UUID(project_id))
                .order_by(Asset.created_at.desc())
                .limit(1)
            )
            if render_row is None:
                raise LookupError(
                    f"no render found for project {project_id} — cannot publish without a finished video"
                )
            render_storage_path = session.get(Asset, render_row.asset_id).storage_path

            thumbnail_row = session.scalar(
                select(Thumbnail)
                .join(Asset, Thumbnail.asset_id == Asset.id)
                .where(Thumbnail.project_id == UUID(project_id), Thumbnail.is_selected.is_(True))
                .order_by(Asset.created_at.desc())
                .limit(1)
            )
            if thumbnail_row is None:
                raise LookupError(
                    f"no selected thumbnail found for project {project_id} — cannot publish without one"
                )
            thumbnail_storage_path = session.get(Asset, thumbnail_row.asset_id).storage_path

            existing_publication = session.scalar(
                select(Publication).where(Publication.project_id == UUID(project_id)).limit(1)
            )
            existing_video_id = existing_publication.youtube_video_id if existing_publication else None
            existing_playlist_id = existing_publication.playlist_id if existing_publication else None

        storage = get_storage_backend()
        video_bytes = storage.read_bytes(render_storage_path)
        thumbnail_bytes = storage.read_bytes(thumbnail_storage_path)

        return _Gathered(
            project_context=project_context,
            script_content=script_content,
            video_bytes=video_bytes,
            thumbnail_bytes=thumbnail_bytes,
            channel_persona_config=persona_config,
            existing_video_id=existing_video_id,
            existing_playlist_id=existing_playlist_id,
        )

    # --- Metadata refinement + playlist resolution --------------------------

    @staticmethod
    def _build_metadata(gathered: _Gathered, payload: dict[str, Any]) -> VideoMetadata:
        project_context = gathered.project_context
        research = project_context.research
        channel = project_context.channel

        refined = MetadataGenerator().generate(
            project_id=project_context.project.project_id,
            video_title=research.topic,
            channel_niche=channel.niche or "general",
            channel_persona=channel.persona or "general audience",
            target_audience=research.target_audience,
            suggested_angle=research.suggested_angle,
            research_keywords=research.keywords,
            draft_description=research.description,
            script_content=gathered.script_content,
        )

        persona_config = gathered.channel_persona_config
        privacy_status = payload.get("privacy_status") or persona_config.get("default_privacy_status") or "private"
        publish_at = payload.get("publish_at")
        category_id = persona_config.get("youtube_category_id", "22")
        playlist_id = PublisherAgent._resolve_playlist_id(
            refined.suggested_playlist_theme, persona_config, payload
        )

        return VideoMetadata(
            title=refined.title,
            description=refined.description,
            tags=refined.tags,
            category_id=category_id,
            privacy_status=privacy_status,
            publish_at=publish_at,
            playlist_id=playlist_id,
        )

    @staticmethod
    def _resolve_playlist_id(
        suggested_theme: str | None, persona_config: dict[str, Any], payload: dict[str, Any]
    ) -> str | None:
        override = payload.get("playlist_id")
        if override:
            return override

        theme_map = persona_config.get("playlist_theme_map") or {}
        if suggested_theme:
            matched = theme_map.get(suggested_theme) or theme_map.get(suggested_theme.lower())
            if matched:
                return matched

        return persona_config.get("default_playlist_id")

    # --- Persistence ---------------------------------------------------------

    @staticmethod
    def _get_or_create_publication(session: Session, project_id: str) -> Publication:
        publication = session.scalar(
            select(Publication).where(Publication.project_id == UUID(project_id)).limit(1)
        )
        if publication is None:
            publication = Publication(
                project_id=UUID(project_id), publish_status=PublishStatus.SCHEDULED, tags=[]
            )
            session.add(publication)
            session.flush()
        return publication

    def _persist_after_upload(
        self, project_id: str, upload_result: UploadResult, metadata: VideoMetadata
    ) -> None:
        with sync_session_scope() as session:
            publication = self._get_or_create_publication(session, project_id)
            publication.youtube_video_id = upload_result.video_id
            publication.youtube_channel_id = upload_result.channel_id
            publication.url = upload_result.url
            publication.uploaded_at = upload_result.uploaded_at
            publication.title = metadata.title
            publication.description = metadata.description
            publication.tags = metadata.tags
            publication.privacy_status = upload_result.privacy_status
            publication.scheduled_at = _parse_rfc3339(metadata.publish_at)
            # Not yet flipped to PUBLISHED — that only happens once
            # verify_upload confirms a healthy status (see run()).
            publication.publish_status = PublishStatus.SCHEDULED

    def _persist_playlist_added(self, project_id: str, playlist_id: str) -> None:
        with sync_session_scope() as session:
            publication = self._get_or_create_publication(session, project_id)
            publication.playlist_id = playlist_id

    def _persist_failure(
        self, project_id: str, video_id: str | None, metadata: VideoMetadata, *, error: str
    ) -> None:
        with sync_session_scope() as session:
            publication = self._get_or_create_publication(session, project_id)
            if video_id:
                publication.youtube_video_id = video_id
            publication.title = metadata.title
            publication.description = metadata.description
            publication.tags = metadata.tags
            publication.privacy_status = metadata.privacy_status
            publication.publish_status = PublishStatus.FAILED
        logger.info("publication_marked_failed", project_id=project_id, video_id=video_id, error=error)

    def _persist_success(
        self,
        project_id: str,
        verified: UploadResult,
        metadata: VideoMetadata,
        playlist_id: str | None,
    ) -> str:
        with sync_session_scope() as session:
            publication = self._get_or_create_publication(session, project_id)
            publication.youtube_video_id = verified.video_id
            publication.youtube_channel_id = verified.channel_id
            publication.url = verified.url
            publication.title = metadata.title
            publication.description = metadata.description
            publication.tags = metadata.tags
            publication.privacy_status = verified.privacy_status
            publication.published_at = _parse_rfc3339(verified.published_at)
            publication.publish_status = (
                PublishStatus.SCHEDULED if metadata.publish_at else PublishStatus.PUBLISHED
            )
            if playlist_id:
                publication.playlist_id = playlist_id
            session.flush()
            return str(publication.id)


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
