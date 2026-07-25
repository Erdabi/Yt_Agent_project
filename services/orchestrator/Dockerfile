# Build context is the repo root (see docker-compose.yml) so this Dockerfile
# can pull in libs/ and the shared requirements file alongside its own code.
FROM python:3.12-slim

WORKDIR /app

# curl: used by the Docker HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt requirements/base.txt
COPY services/orchestrator/requirements.txt services/orchestrator/requirements.txt
RUN pip install --no-cache-dir -r services/orchestrator/requirements.txt

COPY libs/ libs/
# Root-level data directories every service's code may read at runtime —
# not Python code, so not part of `libs/`, but still needed inside the
# image: config/providers.yaml (libs/providers/registry.py) and the
# versioned prompt template files (libs/prompts/registry.py) the Manager
# Agent's reasoning engine loads.
COPY config/ config/
COPY prompts/ prompts/
# Alembic migrations are bundled into this image too: the `migrate`
# Compose service (docker-compose.yml) reuses this same image with
# `command: alembic upgrade head` instead of building a separate one just
# for that.
COPY alembic.ini alembic.ini
COPY migrations/ migrations/
COPY services/orchestrator/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
