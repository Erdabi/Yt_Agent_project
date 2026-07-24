# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
FROM python:3.12-slim

WORKDIR /app

COPY requirements/base.txt requirements/base.txt
COPY services/agent_voiceover/requirements.txt services/agent_voiceover/requirements.txt
RUN pip install --no-cache-dir -r services/agent_voiceover/requirements.txt

COPY libs/ libs/
COPY services/agent_voiceover/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "voiceover", "--concurrency=2"]
