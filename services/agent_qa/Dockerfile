# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
FROM python:3.12-slim

WORKDIR /app

COPY requirements/base.txt requirements/base.txt
COPY services/agent_qa/requirements.txt services/agent_qa/requirements.txt
RUN pip install --no-cache-dir -r services/agent_qa/requirements.txt

COPY libs/ libs/
# config/providers.yaml (libs/providers/registry.py) and the versioned
# prompt template files (libs/prompts/registry.py) this agent will load
# once its real logic lands (docs/architecture/06-roadmap.md, Phase 2).
COPY config/ config/
COPY prompts/ prompts/
COPY services/agent_qa/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "qa", "--concurrency=2"]
