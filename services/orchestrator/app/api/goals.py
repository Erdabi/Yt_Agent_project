"""Where a goal enters the system — the Manager Agent's "receive goals"
responsibility. Submitting a goal creates a project and dispatches the
first pipeline stage (research); everything after that is driven entirely
by services/orchestrator/app/manager and the worker agents reporting back
— this endpoint does not itself run the pipeline, only starts it.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.manager import ManagerAgent

router = APIRouter()
_manager = ManagerAgent()


class GoalIn(BaseModel):
    channel_id: UUID
    goal: str


class GoalOut(BaseModel):
    project_id: UUID


@router.post("/goals", response_model=GoalOut, status_code=202)
def submit_goal(payload: GoalIn) -> GoalOut:
    """Synchronous handler: project creation and job dispatch both use the
    sync DB session and Celery client that the rest of the agent
    framework already shares (see libs/core/db.py) — FastAPI runs sync
    routes in a thread pool automatically, so there's no need to bridge
    this into async code.
    """
    try:
        project_id = _manager.receive_goal(str(payload.channel_id), payload.goal)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return GoalOut(project_id=UUID(project_id))
