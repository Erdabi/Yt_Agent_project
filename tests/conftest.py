"""Shared pytest fixtures.

Every fixture here that touches Postgres is a real integration test
against the actual engine (docs/architecture/05-technology-choices.md
§5.6's testing philosophy — no mocked DB layer) and requires the schema
to already be migrated (`make migrate` / `alembic upgrade head`). A test
that needs Postgres and can't reach one is skipped, not failed, so
`pytest` still runs cleanly with no database configured at all.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    from libs.core.db import get_sync_engine

    try:
        with get_sync_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
def require_postgres(postgres_available: bool) -> None:
    if not postgres_available:
        pytest.skip("no reachable Postgres — set POSTGRES_HOST/etc. or run `make up && make migrate`")


@pytest.fixture(scope="session")
def redis_available() -> bool:
    import redis as redis_sync

    from libs.core.config import get_settings

    settings = get_settings()
    try:
        client = redis_sync.Redis(host=settings.redis_host, port=settings.redis_port)
        return bool(client.ping())
    except Exception:
        return False


@pytest.fixture
def require_redis(redis_available: bool) -> None:
    if not redis_available:
        pytest.skip("no reachable Redis — set REDIS_HOST/etc. or run `make up`")


@pytest.fixture(scope="session")
def client(postgres_available: bool) -> Iterator[TestClient]:
    """One shared `TestClient` (hence one event loop) for the whole test
    session. `app.main`'s async DB engine and the orchestrator's async
    Redis client (libs/core/redis_client.py) are both process-wide
    `@lru_cache`d, tied to whichever event loop first created them — a
    fresh `TestClient` per test spins up its own event loop each time,
    which breaks those cached resources on the second test (`RuntimeError:
    Event loop is closed`). Sharing one instance across every test avoids
    that without touching the app's own caching, which is correct for a
    real running process (exactly one event loop for its whole lifetime).

    Depends on `postgres_available` rather than `require_postgres` (also
    session-scoped, since a session-scoped fixture can't depend on a
    function-scoped one) and does its own skip.
    """
    if not postgres_available:
        pytest.skip("no reachable Postgres — set POSTGRES_HOST/etc. or run `make up && make migrate`")

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
