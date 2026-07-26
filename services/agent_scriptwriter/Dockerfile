# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
FROM python:3.12-slim

WORKDIR /app

COPY requirements/base.txt requirements/base.txt
COPY services/agent_scriptwriter/requirements.txt services/agent_scriptwriter/requirements.txt
RUN pip install --no-cache-dir -r services/agent_scriptwriter/requirements.txt

COPY libs/ libs/
# config/providers.yaml (libs/providers/registry.py, not used by this
# agent yet) and the versioned prompt template files this agent loads via
# libs/prompts/registry.py (prompts/script/generate_script_system/,
# prompts/script/generate_script_user/).
COPY config/ config/
COPY prompts/ prompts/
COPY services/agent_scriptwriter/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "script", "--concurrency=2"]
