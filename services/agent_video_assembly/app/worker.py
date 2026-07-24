"""Video Assembly Agent worker.

Registers the Celery task for the `assembly` queue. The agent's actual
work — compositing voice-over audio, visuals, music, and captions into
the final render via ffmpeg — is not implemented yet. `run()` raises
`NotImplementedError` rather than fabricating a render; see
docs/architecture/03-agent-responsibilities.md §3.6 and
docs/architecture/06-roadmap.md, Phase 1. (ffmpeg and any Python
compositing library are deliberately not installed in this image yet —
see services/agent_video_assembly/requirements.txt — since nothing here
uses them until that implementation lands.)
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class VideoAssemblyAgent(BaseAgent):
    name = "assembly"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Video Assembly agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.6."
        )


@celery_app.task(name="agents.assembly.run", bind=True, base=AgentTask, queue="assembly")
def run_assembly_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return VideoAssemblyAgent().execute_job(job_id)
