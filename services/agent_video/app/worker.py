"""Video Agent worker.

Registers the Celery task for the `video` queue — the Manager dispatches
exactly one job here for the whole `video_creation` stage; see
video_agent.py for why storyboard/voiceover/assembly/thumbnail live here
as internal modules rather than four separate agents.
"""

from typing import Any

from libs.core.celery_app import AgentTask, celery_app

from .video_agent import VideoAgent


@celery_app.task(name="agents.video.run", bind=True, base=AgentTask, queue="video")
def run_video_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return VideoAgent().execute_job(job_id)
