from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.db import get_async_session
from libs.models import Channel

from app.schemas import ChannelOut

router = APIRouter()


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(
    session: AsyncSession = Depends(get_async_session),
) -> list[Channel]:
    result = await session.scalars(select(Channel).order_by(Channel.created_at.desc()))
    return list(result.all())
