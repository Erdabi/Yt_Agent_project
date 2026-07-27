from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.db import get_async_session, sync_session_scope
from libs.models import Channel

from app.schemas import ChannelOut

router = APIRouter()


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    niche: str | None = Field(default=None, max_length=255)
    #: Tone, banned topics, cadence, style guide — see Channel.persona_config's
    #: own docstring (libs/models/channel.py). Deliberately unstructured here
    #: too, for the same reason: it evolves per-channel without needing an
    #: API/schema change each time.
    persona_config: dict = Field(default_factory=dict)
    youtube_channel_id: str | None = Field(default=None, max_length=64)


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(
    session: AsyncSession = Depends(get_async_session),
) -> list[Channel]:
    result = await session.scalars(select(Channel).order_by(Channel.created_at.desc()))
    return list(result.all())


@router.post("/channels", response_model=ChannelOut, status_code=201)
def create_channel(payload: ChannelIn) -> Channel:
    """The only way to create a channel before this endpoint existed was a
    direct database write — every other write path in this API
    (`POST /goals`) needs an existing `channel_id` before it can do
    anything, so this is a genuine onboarding gap being closed, not an
    optional convenience.

    A plain sync `def` using `sync_session_scope()`, matching
    `goals.py`'s `submit_goal` — the one other creation endpoint in this
    API — rather than the async session `list_channels` above uses:
    FastAPI already runs sync routes in a thread pool, so there's no
    async session/commit-timing subtlety to reason about for a single
    insert, and both write paths in this API now follow the identical
    pattern.
    """
    with sync_session_scope() as session:
        channel = Channel(
            name=payload.name,
            niche=payload.niche,
            persona_config=payload.persona_config,
            youtube_channel_id=payload.youtube_channel_id,
        )
        session.add(channel)
        # Populates the server-generated `created_at` (server_default=func.now())
        # before the response is serialized — `id` already has a Python-side
        # default (UUIDPrimaryKeyMixin), but created_at does not.
        session.flush()
    return channel
