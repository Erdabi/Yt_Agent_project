"""Script Writing Agent worker.

Turns an approved, researched video idea into a complete,
production-ready script: a strong opening hook, an introduction, a
deliberately chosen story structure, the main body sections, retention
techniques woven throughout, an ending, and a call to action — each beat
carrying voice-over text, a scene description, and structured production
metadata (camera framing, visual asset type, transition, pacing,
narration emotion, emphasis words, speech speed, on-screen text). See
docs/architecture/03-agent-responsibilities.md §3.3.

Loads everything it needs through the Project Context Builder
(`libs.context.build_project_context`) instead of separately querying the
channel, project, idea, and knowledge package itself — see
libs/context/builder.py. Generation (script_generator.py) drafts the
script; a self-review pass (script_reviewer.py) checks it for factual
consistency against the Knowledge Package, viewer retention, and
repetition, and returns an improved version — always run before anything
is persisted. This module's own job is dispatch plumbing and persistence:
turning the final `GeneratedScript` into a versioned `scripts` row plus
its ordered `script_segments`, fully structured (including each
segment's `production_metadata`) so the Video Agent can consume it
directly without further parsing.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from libs.agents.base import BaseAgent
from libs.context import ProjectContext, build_project_context
from libs.core.celery_app import AgentTask, celery_app
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.enums import ScriptSegmentType, ScriptStatus
from libs.models.script import Script, ScriptSegment
from libs.schemas.jobs import JobContext

from .script_generator import ScriptGenerator
from .script_reviewer import ScriptReviewer
from .script_schema import GeneratedScript, ScriptBeat

logger = get_logger(__name__)

#: `SegmentProductionMetadata.estimated_speech_wpm` is bounded the same
#: way in the tool schema (script_schema.py), but this is Claude-supplied
#: data feeding a duration calculation — clamped again here rather than
#: trusted outright, same defensive posture as the Manager enforcing its
#: own retry ceiling instead of trusting the reasoning engine's arithmetic
#: (services/orchestrator/app/manager/manager.py).
_MIN_SPEECH_WPM = 80
_MAX_SPEECH_WPM = 220

#: (agent, prompt name) passed to `build_project_context` to resolve
#: `ProjectContext.prompt_version` — this agent's own prompt slot (see
#: prompts/script/generate_script_system/). The review prompt slot
#: (prompts/script/review_script_system/) is versioned in lockstep with
#: this one under the same SCRIPT_PROMPT_VERSION setting, so one resolved
#: version serves both calls.
_CONSUMER_PROMPT = ("script", "generate_script_system")


class ScriptwriterAgent(BaseAgent):
    name = "script"

    def __init__(self) -> None:
        self._generator = ScriptGenerator()
        self._reviewer = ScriptReviewer()

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Script Agent requires a project_id — it is always dispatched "
                "against an existing project by the Manager Agent's workflow plan"
            )

        project_context = build_project_context(context.project_id, consumer_prompt=_CONSUMER_PROMPT)
        draft = self._generator.generate(project_context)
        final = self._reviewer.review(project_context, draft)
        script_id, version = self._store_script(context.project_id, project_context, final)

        logger.info(
            "script_generated",
            project_id=context.project_id,
            script_id=script_id,
            version=version,
            section_count=len(final.main_sections),
        )
        return {
            "script_id": script_id,
            "version": version,
            "structure_notes": final.structure_notes,
            "retention_notes": final.retention_notes,
            "review_notes": final.review_notes,
            "section_count": len(final.main_sections),
        }

    def _store_script(
        self, project_id: str, project_context: ProjectContext, generated: GeneratedScript
    ) -> tuple[str, int]:
        beats: list[tuple[ScriptSegmentType, ScriptBeat, str | None]] = [
            (ScriptSegmentType.HOOK, generated.hook, None),
            (ScriptSegmentType.INTRODUCTION, generated.introduction, None),
        ]
        beats.extend(
            (ScriptSegmentType.MAIN_SECTION, section, section.heading)
            for section in generated.main_sections
        )
        beats.append((ScriptSegmentType.ENDING, generated.ending, None))
        beats.append((ScriptSegmentType.CALL_TO_ACTION, generated.call_to_action, None))

        full_text = "\n\n".join(beat.voiceover_text for _, beat, _ in beats)
        word_count = len(full_text.split())

        with sync_session_scope() as session:
            # Scripts are versioned rows, never mutated in place (see
            # libs/models/script.py) — a QA-triggered regeneration gets
            # the next version number, not an overwrite of a prior one.
            current_max_version = session.scalar(
                select(func.coalesce(func.max(Script.version), 0)).where(
                    Script.project_id == UUID(project_id)
                )
            )
            next_version = current_max_version + 1

            script = Script(
                project_id=UUID(project_id),
                version=next_version,
                content=full_text,
                tone=project_context.channel.persona,
                target_duration_sec=project_context.research.suggested_length_sec,
                word_count=word_count,
                structure_notes=generated.structure_notes,
                retention_notes=generated.retention_notes,
                review_notes=generated.review_notes,
                status=ScriptStatus.DRAFT,
            )
            session.add(script)
            session.flush()

            for order_index, (segment_type, beat, heading) in enumerate(beats):
                scene_notes = f"[{heading}] {beat.scene_description}" if heading else beat.scene_description
                words = len(beat.voiceover_text.split())
                wpm = max(_MIN_SPEECH_WPM, min(_MAX_SPEECH_WPM, beat.production.estimated_speech_wpm))
                session.add(
                    ScriptSegment(
                        script_id=script.id,
                        order_index=order_index,
                        segment_type=segment_type,
                        text=beat.voiceover_text,
                        scene_notes=scene_notes,
                        visual_notes=beat.visual_suggestions,
                        estimated_duration_sec=max(1, round(words / wpm * 60)),
                        production_metadata=beat.production.model_dump(mode="json"),
                    )
                )

            script_id = str(script.id)

        return script_id, next_version


@celery_app.task(name="agents.script.run", bind=True, base=AgentTask, queue="script")
def run_script_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ScriptwriterAgent().execute_job(job_id)
