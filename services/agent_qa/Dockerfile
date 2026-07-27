# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
FROM python:3.12-slim

WORKDIR /app

# ffmpeg/ffprobe back the Quality Control Agent's real technical
# inspection of the rendered video (app/media_inspection.py) — resolution/
# duration/decode checks, silence/clipping/black-frame detection. No
# fonts needed: unlike the Video Agent, nothing here renders text.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt requirements/base.txt
COPY services/agent_qa/requirements.txt services/agent_qa/requirements.txt
RUN pip install --no-cache-dir -r services/agent_qa/requirements.txt

COPY libs/ libs/
# config/providers.yaml (libs/providers/registry.py — the `llm` capability)
# and the versioned prompt template files (libs/prompts/registry.py,
# prompts/qa/) this agent's reviewers load.
COPY config/ config/
COPY prompts/ prompts/
COPY services/agent_qa/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "qa", "--concurrency=2"]
