from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.db import get_async_session
from libs.models.enums import JobStatus
from libs.models.job import Job

from app.schemas import JobOut

router = APIRouter()


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    status: JobStatus | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    session: AsyncSession = Depends(get_async_session),
) -> list[Job]:
    """The primary debugging view over the audit trail every agent writes
    to (docs/architecture/04-database-design.md, `jobs` table).
    """
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    result = await session.scalars(stmt)
    return list(result.all())
