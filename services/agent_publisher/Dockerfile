# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
FROM python:3.12-slim

WORKDIR /app

COPY requirements/base.txt requirements/base.txt
COPY services/agent_publisher/requirements.txt services/agent_publisher/requirements.txt
RUN pip install --no-cache-dir -r services/agent_publisher/requirements.txt

COPY libs/ libs/
# config/providers.yaml (libs/providers/registry.py — the `youtube` and
# `llm` capabilities). prompts/ is needed too now: metadata_generator.py
# renders prompts/publish/refine_metadata_{system,user}/ for its one LLM
# call (title/description/tags/playlist-theme refinement) — the rest of
# this agent's work (upload, thumbnail, playlist, verify) is YouTube Data
# API mechanics with no prompt of its own.
COPY config/ config/
COPY prompts/ prompts/
COPY services/agent_publisher/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "publish", "--concurrency=2"]
