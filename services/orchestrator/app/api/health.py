"""A real liveness/readiness check — actually queries Postgres and pings
Redis rather than returning a hardcoded 200, so it's meaningful as a Docker
Compose `healthcheck` target or an external monitor's probe.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from libs.core.db import check_database_connection
from libs.core.redis_client import check_redis_connection

from app.schemas import HealthOut

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    database_ok = await check_database_connection()
    redis_ok = await check_redis_connection()
    healthy = database_ok and redis_ok

    payload = HealthOut(
        status="ok" if healthy else "degraded",
        database=database_ok,
        redis=redis_ok,
    )
    return JSONResponse(content=payload.model_dump(), status_code=200 if healthy else 503)
