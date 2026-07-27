"""Video Agent worker.

Registers two Celery tasks on the `video` queue:

- `agents.video.run` — the Manager dispatches exactly one job here for
  the whole `video_creation` stage; see video_agent.py for why Asset
  Planning, Asset Generation, Voice Generation, Subtitle Generation,
  Timeline Building, and Rendering live here as internal modules rather
  than separate agents (the Thumbnail Agent is invoked in-process by that
  same job too — see thumbnail_agent.py for why).
- `agents.thumbnail.run` — a standalone entry point for the Thumbnail
  Agent, dispatched independently of the Manager's workflow (e.g.
  regenerating a thumbnail without rerunning the whole video pipeline);
  same two-tasks-in-one-worker.py precedent as
  services/agent_research/app/worker.py's `agents.research.discover`.
"""

from typing import Any

from libs.core.celery_app import AgentTask, celery_app

from .thumbnail_agent import ThumbnailAgent
from .video_agent import VideoAgent


@celery_app.task(name="agents.video.run", bind=True, base=AgentTask, queue="video")
def run_video_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return VideoAgent().execute_job(job_id)


@celery_app.task(name="agents.thumbnail.run", bind=True, base=AgentTask, queue="video")
def run_thumbnail_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ThumbnailAgent().execute_job(job_id)
