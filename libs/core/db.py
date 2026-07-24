"""Database engine and session management.

Two separate engines exist, both pointed at the same PostgreSQL database,
because the orchestrator and the agent workers fit different concurrency
models:

- The orchestrator is a FastAPI app with async request handlers, so it
  gets an async engine (`asyncpg` driver).
- Agent workers are Celery tasks, which run synchronously in prefork
  worker processes. Driving async DB calls from inside a sync task means
  spinning up an event loop per task for no real benefit, so agents get a
  plain synchronous engine (`psycopg` driver) instead.

See `libs.core.config.Settings.database_url` / `.sync_database_url`.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from libs.core.config import get_settings


@lru_cache
def get_async_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
    )


@lru_cache
def get_async_sessionmaker() -> async_sessionmaker:
    return async_sessionmaker(get_async_engine(), expire_on_commit=False)


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """`async with async_session_scope() as session: ...` — commits on
    success, rolls back on exception. Used by orchestrator code.
    """
    session_factory = get_async_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: `session: AsyncSession = Depends(get_async_session)`."""
    async with async_session_scope() as session:
        yield session


@lru_cache
def get_sync_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.sync_database_url,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
    )


@lru_cache
def get_sync_sessionmaker() -> sessionmaker:
    return sessionmaker(get_sync_engine(), expire_on_commit=False)


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """`with sync_session_scope() as session: ...` — commits on success,
    rolls back on exception. Used by the agent framework (libs/agents/base.py)
    and every Celery task.
    """
    session_factory = get_sync_sessionmaker()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def check_database_connection() -> bool:
    """A real connectivity check (`SELECT 1`), not a hardcoded True — used
    by the orchestrator's `/health` endpoint.
    """
    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
