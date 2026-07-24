"""Voice-over Agent worker.

Registers the Celery task for the `voiceover` queue. The agent's actual
work — sending each script segment to a TTS provider, normalizing
loudness, capturing word-level timestamps for caption sync — is not
implemented yet. `run()` raises `NotImplementedError` rather than
fabricating audio; see docs/architecture/03-agent-responsibilities.md §3.5
and docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class VoiceoverAgent(BaseAgent):
    name = "voiceover"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Voice-over agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.5."
        )


@celery_app.task(name="agents.voiceover.run", bind=True, base=AgentTask, queue="voiceover")
def run_voiceover_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return VoiceoverAgent().execute_job(job_id)
