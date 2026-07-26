"""Script Writing Agent worker.

Turns an approved, researched video idea into a complete,
production-ready script: a strong opening hook, an introduction, a
deliberately chosen story structure, the main body sections, retention
techniques woven throughout, an ending, and a call to action — each beat
carrying voice-over text, a scene description, and visual suggestions.
See docs/architecture/03-agent-responsibilities.md §3.3.

Loads everything it needs through the Project Context Builder
(`libs.context.build_project_context`) instead of separately querying the
channel, project, idea, and knowledge package itself — see
libs/context/builder.py. The actual generation call lives in
script_generator.py; this module's job is dispatch plumbing and
persistence: turning a `GeneratedScript` into a versioned `scripts` row
plus its ordered `script_segments`.
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

from .script_generator import GeneratedScript, ScriptBeat, ScriptGenerator

logger = get_logger(__name__)

#: Rough narration pace used to estimate each segment's spoken duration
#: from its word count. Not a precise prediction — the real duration
#: comes from the Voice-over module once actual audio exists
#: (services/agent_video/app/modules/voiceover.py) — just enough for the
#: Storyboard module to plan shot lengths before that audio exists.
_WORDS_PER_MINUTE = 150

#: (agent, prompt name) passed to `build_project_context` to resolve
#: `ProjectContext.prompt_version` — this agent's own prompt slot (see
#: prompts/script/generate_script_system/).
_CONSUMER_PROMPT = ("script", "generate_script_system")


class ScriptwriterAgent(BaseAgent):
    name = "script"

    def __init__(self) -> None:
        self._generator = ScriptGenerator()

    def run(self, context: JobContext) -> dict[str, Any]:
        if not context.project_id:
            raise ValueError(
                "the Script Agent requires a project_id — it is always dispatched "
                "against an existing project by the Manager Agent's workflow plan"
            )

        project_context = build_project_context(context.project_id, consumer_prompt=_CONSUMER_PROMPT)
        generated = self._generator.generate(project_context)
        script_id, version = self._store_script(context.project_id, project_context, generated)

        logger.info(
            "script_generated",
            project_id=context.project_id,
            script_id=script_id,
            version=version,
            section_count=len(generated.main_sections),
        )
        return {
            "script_id": script_id,
            "version": version,
            "structure_notes": generated.structure_notes,
            "retention_notes": generated.retention_notes,
            "section_count": len(generated.main_sections),
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
                status=ScriptStatus.DRAFT,
            )
            session.add(script)
            session.flush()

            for order_index, (segment_type, beat, heading) in enumerate(beats):
                scene_notes = f"[{heading}] {beat.scene_description}" if heading else beat.scene_description
                words = len(beat.voiceover_text.split())
                session.add(
                    ScriptSegment(
                        script_id=script.id,
                        order_index=order_index,
                        segment_type=segment_type,
                        text=beat.voiceover_text,
                        scene_notes=scene_notes,
                        visual_notes=beat.visual_suggestions,
                        estimated_duration_sec=max(1, round(words / _WORDS_PER_MINUTE * 60)),
                    )
                )

            script_id = str(script.id)

        return script_id, next_version


@celery_app.task(name="agents.script.run", bind=True, base=AgentTask, queue="script")
def run_script_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ScriptwriterAgent().execute_job(job_id)
