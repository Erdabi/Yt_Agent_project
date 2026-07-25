# Build context is the repo root (see docker-compose.yml) so this
# Dockerfile can pull in libs/ and the shared requirements file alongside
# its own code, matching every other service in services/.
#
# This one image replaces what used to be four separate services
# (agent_storyboard, agent_voiceover, agent_video_assembly,
# agent_thumbnail) — storyboard, voice-over, assembly, and thumbnail
# generation are now internal modules of one Video Agent (app/video_agent.py),
# run as a single job on the `video` queue.
FROM python:3.12-slim

WORKDIR /app

COPY requirements/base.txt requirements/base.txt
COPY services/agent_video/requirements.txt services/agent_video/requirements.txt
RUN pip install --no-cache-dir -r services/agent_video/requirements.txt

COPY libs/ libs/
COPY services/agent_video/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "video", "--concurrency=2"]
