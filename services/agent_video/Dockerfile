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

# ffmpeg/ffprobe back the Rendering module's real compositor
# (libs/providers/editor/ffmpeg_provider.py); fonts-dejavu-core supplies
# the font its drawtext filter (captions, on-screen text, thumbnail
# overlay) renders with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt requirements/base.txt
COPY services/agent_video/requirements.txt services/agent_video/requirements.txt
RUN pip install --no-cache-dir -r services/agent_video/requirements.txt

COPY libs/ libs/
# config/providers.yaml (libs/providers/registry.py — image_gen/tts/video_gen
# capabilities) and the versioned prompt template files
# (libs/prompts/registry.py) this agent's modules will load once their
# real logic lands (docs/architecture/06-roadmap.md, Phase 1).
COPY config/ config/
COPY prompts/ prompts/
COPY services/agent_video/app app/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=info", "-Q", "video", "--concurrency=2"]
