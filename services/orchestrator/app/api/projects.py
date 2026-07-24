from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.db import get_async_session
from libs.models import Project
from libs.models.enums import ProjectStatus

from app.schemas import ProjectOut

router = APIRouter()


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    status: ProjectStatus | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    session: AsyncSession = Depends(get_async_session),
) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Project.status == status)
    result = await session.scalars(stmt)
    return list(result.all())


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: UUID,
    session: AsyncSession = Depends(get_async_session),
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project
