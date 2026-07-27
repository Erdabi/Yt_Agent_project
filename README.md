# Yt Agent Project

An autonomous AI system that researches ideas, writes scripts, generates
voice-over and visuals, assembles video, runs quality checks, uploads to
YouTube, and tracks performance — deployed as Docker Compose services on a
Hetzner Cloud VPS.

The pipeline is implemented end to end: an Orchestrator (FastAPI + a Manager
Agent) drives six worker agents — Research, Script, Video (storyboard,
voice-over, assembly, and thumbnail as internal modules), Quality Control,
Publisher, and an independently-scheduled Analytics agent — through Celery,
against a PostgreSQL schema with full migration history. Every AI capability
(LLM reasoning, TTS, image/video generation, stock media, audio library,
video compositing, YouTube publishing) sits behind a swappable provider
interface (`libs/providers/`), config-driven via `config/providers.yaml`, so
a vendor is a config change, not a code change. See
[docs/architecture/06-roadmap.md](./docs/architecture/06-roadmap.md) for
exactly what's done vs. still planned per phase.

## Getting started

```bash
cp .env.example .env        # fill in real secrets before running for real
make up                      # build + start postgres, redis, orchestrator, all agents
make migrate                 # apply the Alembic schema to the running Postgres
```

The Orchestrator's API is then reachable on the port configured in
`docker-compose.yml`. Since submitting a goal requires an existing channel,
and there is no seed data, the first real request is always:

```bash
# 1. Create a channel
curl -X POST localhost:8000/channels \
  -H "Content-Type: application/json" \
  -d '{"name": "My Channel", "niche": "science education"}'
# -> {"id": "...", "name": "My Channel", ...}

# 2. Submit a goal against it — this creates a project and dispatches the
#    first pipeline stage (research); everything after that is driven by
#    the Manager and the worker agents reporting back.
curl -X POST localhost:8000/goals \
  -H "Content-Type: application/json" \
  -d '{"channel_id": "<id from step 1>", "goal": "a video about how engines work"}'
# -> {"project_id": "..."}

# 3. Watch it move through the pipeline
curl localhost:8000/projects/<project_id>
curl "localhost:8000/jobs?limit=20"
```

See [docs/architecture/01-system-architecture.md §1.4](./docs/architecture/01-system-architecture.md)
for the full API surface and pipeline state machine.

## Running tests

```bash
pip install -r requirements/test.txt
pytest
```

Tests that need a real Postgres (see `tests/conftest.py`) are skipped
automatically if `POSTGRES_HOST`/`POSTGRES_DB`/etc. aren't reachable —
`make up` starts one, or point the env vars at any Postgres 16+ instance.

## Documentation

Start here: **[docs/architecture/README.md](./docs/architecture/README.md)**,
which links to:

1. [System Architecture](./docs/architecture/01-system-architecture.md)
2. [Folder Structure](./docs/architecture/02-folder-structure.md)
3. [Agent Responsibilities](./docs/architecture/03-agent-responsibilities.md)
4. [Database Design](./docs/architecture/04-database-design.md)
5. [Technology Choices](./docs/architecture/05-technology-choices.md)
6. [Development Roadmap](./docs/architecture/06-roadmap.md)
