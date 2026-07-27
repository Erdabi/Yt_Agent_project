"""The Quality Control Agent.

Inspects a project's entire finished production — script, video, audio,
subtitles, thumbnail — and decides whether it's ready to publish.

    Input:  JobContext (project_id)
    Output: dict (verdict, report id, per-category pass/fail)

Coordinates specialized reviewers (reviewers/) rather than putting all
evaluation logic in one class: this agent's own job is gathering the
production's data once (`_gather`), handing the same immutable
`ReviewInput` to every reviewer, aggregating their independent verdicts
into one APPROVED/REJECTED decision, and persisting the result. Each
reviewer owns one category's checks and knows nothing about any other
reviewer or about persistence — see reviewers/base.py's `Reviewer`
interface. `self._reviewers` is a plain list; adding a sixth reviewer
later (e.g. a content-policy reviewer building on the existing
prompts/qa/policy_review/ draft — docs/architecture/03-agent-responsibilities.md
§3.8) means writing one new class and appending it here, nothing else in
this file or any existing reviewer changes.

Architecture — reuse, not reinvention:
- **Provider Registry** (`libs.providers.get_provider("llm")`) — every
  reviewer's LLM call goes through this, not a hardcoded `anthropic`
  import (see libs/providers/llm/base.py's own docstring for why this
  agent is the first to do that, unlike Research/Script/the Thumbnail
  Agent's own direct SDK calls).
- **Prompt Management** (`libs.prompts`) — every reviewer's prompts live
  at prompts/qa/{script,video,thumbnail}_review_{system,user}/, versioned
  and pinnable via `QA_PROMPT_VERSION`.
- **Project Context** (`libs.context.build_project_context`) — supplies
  the channel profile and research summary without this agent
  re-querying `Channel`/`VideoIdea` itself. Script/video/audio/subtitle/
  thumbnail production data isn't part of `ProjectContext` (it predates
  all of it — see that module's own docstring), so `_gather` reads it
  directly, the same scoping decision the Thumbnail Agent already makes
  for its own script query.
- **LLM usage tracking** (`libs.llm_usage.track_llm_call`) — every
  reviewer's LLM call is wrapped individually (see llm_review.py), so
  the usage log shows exactly which category/call made each request.

`reports_to_manager` stays at `BaseAgent`'s default (`True`): unlike the
Thumbnail Agent's standalone regeneration path, this agent *is* the
Manager's `quality_check` stage
(services/orchestrator/app/manager/workflow.py) — its completion should
always drive the Manager's advance/retry/escalate decision for
`QA_REVIEW`.
"""

import os
import tempfile
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from libs.agents.base import BaseAgent
from libs.context import build_project_context
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.asset import Asset, Render, Thumbnail, Voiceover
from libs.models.enums import ProjectStage
from libs.models.qa import QAReport
from libs.models.script import Script, ScriptSegment
from libs.models.storyboard import StoryboardShot
from libs.providers.tts.base import WordTiming
from libs.schemas.jobs import JobContext
from libs.schemas.script_production import SegmentProductionMetadata
from libs.storage import get_storage_backend

from .qa_schema import (
    ReviewInput,
    ReviewResult,
    RenderData,
    SegmentData,
    StoryboardShotData,
    ThumbnailData,
    VoiceoverData,
)
from .reviewers.audio_reviewer import AudioReviewer
from .reviewers.base import Reviewer
from .reviewers.script_reviewer import ScriptReviewer
from .reviewers.subtitle_reviewer import SubtitleReviewer
from .reviewers.thumbnail_reviewer import ThumbnailReviewer
from .reviewers.video_reviewer import VideoReviewer

logger = get_logger(__name__)


@dataclass(frozen=True)
class _RenderRef:
    asset_id: str
    storage_path: str
    resolution: str | None
    duration_sec: float | None
    render_engine: str | None
    profile_name: str | None


@dataclass(frozen=True)
class _ThumbnailRef:
    asset_id: str
    storage_path: str
    variant_label: str | None
    is_selected: bool
    prompt: str | None
    concept_name: str | None
    overlay_text: str | None
    provider: str | None


class QualityControlAgent(BaseAgent):
    name = "qa"

    def __init__(self) -> None:
        self._storage = get_storage_backend()
        self._reviewers: list[Reviewer] = [
            ScriptReviewer(),
            VideoReviewer(),
            AudioReviewer(),
            SubtitleReviewer(),
            ThumbnailReviewer(),
        ]

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Quality Control Agent requires a project_id — it is always "
                "dispatched against an existing project"
            )

        with tempfile.TemporaryDirectory(prefix="qa_inspection_") as work_dir:
            review_input = self._gather(context.project_id, work_dir)
            results = [reviewer.review(review_input) for reviewer in self._reviewers]

        verdict, issues, metadata = self._aggregate(results)
        report_id = self._persist(context.project_id, verdict, issues, metadata)

        logger.info(
            "quality_control_complete",
            project_id=context.project_id,
            verdict=verdict,
            issue_count=len(issues),
            report_id=report_id,
        )
        return {
            "report_id": report_id,
            "verdict": verdict,
            "issue_count": len(issues),
            "categories": {result.category: result.passed for result in results},
        }

    # --- Aggregation + persistence ------------------------------------------

    @staticmethod
    def _aggregate(results: list[ReviewResult]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        verdict = "APPROVED" if all(result.passed for result in results) else "REJECTED"
        issues = [
            {
                "category": issue.category,
                "severity": issue.severity,
                "detail": issue.detail,
                "suggested_fix": issue.suggested_fix,
            }
            for result in results
            for issue in result.issues
        ]
        metadata = {
            "reviewers": {
                result.category: {
                    "passed": result.passed,
                    "summary": result.summary,
                    "issue_count": len(result.issues),
                }
                for result in results
            },
        }
        return verdict, issues, metadata

    @staticmethod
    def _persist(project_id: str, verdict: str, issues: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
        with sync_session_scope() as session:
            report = QAReport(
                project_id=UUID(project_id),
                stage=ProjectStage.QA_REVIEW.value,
                passed=verdict == "APPROVED",
                issues=issues,
                metadata_=metadata,
            )
            session.add(report)
            session.flush()
            return str(report.id)

    # --- Gathering: everything every reviewer might need, read once --------

    def _gather(self, project_id: str, work_dir: str) -> ReviewInput:
        project_context = build_project_context(
            project_id, consumer_prompt=("qa", "script_review_system")
        )

        with sync_session_scope() as session:
            script = session.scalar(
                select(Script)
                .where(Script.project_id == UUID(project_id))
                .order_by(Script.version.desc())
                .limit(1)
            )
            if script is None:
                raise LookupError(f"no script found for project {project_id}")
            script_content = script.content
            script_target_duration_sec = script.target_duration_sec

            segment_rows = session.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.script_id == script.id)
                .order_by(ScriptSegment.order_index)
            ).all()
            segments = [self._segment_data(session, row) for row in segment_rows]

            render_ref = self._render_ref(session, project_id)
            thumbnail_ref = self._thumbnail_ref(session, project_id)

        render_data = self._resolve_render(render_ref, work_dir)
        thumbnail_data = self._resolve_thumbnail(thumbnail_ref)

        return ReviewInput(
            project_context=project_context,
            video_title=project_context.research.topic,
            script_content=script_content,
            script_target_duration_sec=script_target_duration_sec,
            segments=segments,
            render=render_data,
            thumbnail=thumbnail_data,
        )

    @staticmethod
    def _segment_data(session, row: ScriptSegment) -> SegmentData:
        voiceover_row = session.scalar(
            select(Voiceover).where(Voiceover.script_segment_id == row.id).limit(1)
        )
        voiceover_data: VoiceoverData | None = None
        if voiceover_row is not None:
            asset = session.get(Asset, voiceover_row.asset_id)
            word_timings_raw = ((asset.metadata_ if asset else {}) or {}).get("word_timings") or []
            voiceover_data = VoiceoverData(
                asset_id=str(voiceover_row.asset_id),
                storage_path=asset.storage_path if asset else "",
                duration_sec=float(voiceover_row.duration_sec or 0.0),
                provider=voiceover_row.provider,
                model=voiceover_row.model,
                voice_id=voiceover_row.voice_id,
                language=voiceover_row.language,
                word_timings=[
                    WordTiming(word=item["word"], start_sec=item["start_sec"], end_sec=item["end_sec"])
                    for item in word_timings_raw
                ],
            )

        shot_rows = session.scalars(
            select(StoryboardShot)
            .where(StoryboardShot.script_segment_id == row.id)
            .order_by(StoryboardShot.order_index)
        ).all()
        storyboard_shots = [
            StoryboardShotData(
                shot_type=shot.shot_type.value,
                prompt_or_query=shot.prompt_or_query,
                asset_id=str(shot.asset_id) if shot.asset_id else None,
                order_index=shot.order_index,
            )
            for shot in shot_rows
        ]

        return SegmentData(
            segment_id=str(row.id),
            order_index=row.order_index,
            segment_type=row.segment_type.value,
            text=row.text,
            production_metadata=SegmentProductionMetadata.model_validate(row.production_metadata),
            voiceover=voiceover_data,
            storyboard_shots=storyboard_shots,
        )

    @staticmethod
    def _render_ref(session, project_id: str) -> _RenderRef | None:
        render_row = session.scalar(
            select(Render)
            .join(Asset, Render.asset_id == Asset.id)
            .where(Render.project_id == UUID(project_id))
            .order_by(Asset.created_at.desc())
            .limit(1)
        )
        if render_row is None:
            return None
        render_asset = session.get(Asset, render_row.asset_id)
        return _RenderRef(
            asset_id=str(render_row.asset_id),
            storage_path=render_asset.storage_path,
            resolution=render_row.resolution,
            duration_sec=float(render_row.duration_sec) if render_row.duration_sec is not None else None,
            render_engine=render_row.render_engine,
            profile_name=(render_asset.metadata_ or {}).get("profile_name"),
        )

    @staticmethod
    def _thumbnail_ref(session, project_id: str) -> _ThumbnailRef | None:
        thumbnail_row = session.scalar(
            select(Thumbnail)
            .join(Asset, Thumbnail.asset_id == Asset.id)
            .where(Thumbnail.project_id == UUID(project_id), Thumbnail.is_selected.is_(True))
            .order_by(Asset.created_at.desc())
            .limit(1)
        )
        if thumbnail_row is None:
            return None
        thumbnail_asset = session.get(Asset, thumbnail_row.asset_id)
        metadata = thumbnail_asset.metadata_ or {}
        return _ThumbnailRef(
            asset_id=str(thumbnail_row.asset_id),
            storage_path=thumbnail_asset.storage_path,
            variant_label=thumbnail_row.variant_label,
            is_selected=thumbnail_row.is_selected,
            prompt=metadata.get("prompt"),
            concept_name=metadata.get("concept_name"),
            overlay_text=metadata.get("overlay_text"),
            provider=thumbnail_asset.provider,
        )

    def _resolve_render(self, render_ref: _RenderRef | None, work_dir: str) -> RenderData | None:
        if render_ref is None:
            return None
        local_path = os.path.join(work_dir, "render.mp4")
        with open(local_path, "wb") as f:
            f.write(self._storage.read_bytes(render_ref.storage_path))
        return RenderData(
            asset_id=render_ref.asset_id,
            storage_path=render_ref.storage_path,
            local_path=local_path,
            resolution=render_ref.resolution,
            duration_sec=render_ref.duration_sec,
            render_engine=render_ref.render_engine,
            profile_name=render_ref.profile_name,
        )

    def _resolve_thumbnail(self, thumbnail_ref: _ThumbnailRef | None) -> ThumbnailData | None:
        if thumbnail_ref is None:
            return None
        return ThumbnailData(
            asset_id=thumbnail_ref.asset_id,
            storage_path=thumbnail_ref.storage_path,
            image_bytes=self._storage.read_bytes(thumbnail_ref.storage_path),
            variant_label=thumbnail_ref.variant_label,
            is_selected=thumbnail_ref.is_selected,
            prompt=thumbnail_ref.prompt,
            concept_name=thumbnail_ref.concept_name,
            overlay_text=thumbnail_ref.overlay_text,
            provider=thumbnail_ref.provider,
        )
