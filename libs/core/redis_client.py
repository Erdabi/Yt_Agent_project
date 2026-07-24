"""Direct async Redis access, for services that need to talk to Redis
outside of Celery's own connection handling — today, just the
orchestrator's `/health` endpoint. Agent workers talk to Redis exclusively
through Celery (see libs/core/celery_app.py) and don't use this module.
"""

from functools import lru_cache

import redis.asyncio as redis

from libs.core.config import get_settings


@lru_cache
def get_redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)


async def check_redis_connection() -> bool:
    """A real `PING`, not a hardcoded True — used by the orchestrator's
    `/health` endpoint.
    """
    try:
        client = get_redis_client()
        return bool(await client.ping())
    except Exception:
        return False
