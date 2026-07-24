# 2. Folder Structure

Monorepo, one repository containing every service plus shared libraries. This
keeps cross-service refactors (e.g. changing a shared DTO) a single PR, while
each service still builds its own independent Docker image.

```
yt-agent/
├── docker-compose.yml                # base stack: postgres, redis, minio, caddy, orchestrator, all agents
├── docker-compose.override.yml.example
├── .env.example                      # documents every required env var, no real secrets
├── Makefile                          # make up / down / logs / migrate / test
├── README.md
│
├── docs/
│   ├── architecture/                 # this design doc set
│   └── adr/                          # architecture decision records (one file per significant decision)
│
├── services/                         # one folder per deployable container
│   ├── orchestrator/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── app/
│   │       ├── main.py               # FastAPI app (internal API + dashboard backend)
│   │       ├── state_machine.py      # pipeline stage transitions + retry policy
│   │       ├── scheduler.py          # Celery beat schedules (ideation cadence, analytics pulls)
│   │       ├── approval_gates.py     # config-driven human-in-the-loop rules
│   │       └── api/                  # routers: projects, ideas, jobs, approvals
│   │
│   ├── agent_research/
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── worker.py             # Celery task entrypoint
│   │       ├── trend_sources/        # youtube_trending.py, google_trends.py, reddit.py, rss.py
│   │       ├── scoring.py            # LLM-based idea scoring + dedup via embeddings
│   │       └── prompts/
│   │
│   ├── agent_scriptwriter/
│   │   └── app/{worker.py, prompts/, fact_check.py}
│   │
│   ├── agent_storyboard/
│   │   └── app/{worker.py, shot_planner.py, stock_search.py}
│   │
│   ├── agent_voiceover/
│   │   └── app/{worker.py, ssml_builder.py, loudness_normalize.py}
│   │
│   ├── agent_video_assembly/
│   │   └── app/{worker.py, compositor.py, captions.py, ffmpeg_pipeline.py}
│   │
│   ├── agent_thumbnail/
│   │   └── app/{worker.py, text_overlay.py, style_guide.py}
│   │
│   ├── agent_qa/
│   │   └── app/{worker.py, technical_checks.py, policy_review.py}
│   │
│   ├── agent_publisher/
│   │   └── app/{worker.py, youtube_upload.py, oauth_token_manager.py, quota_guard.py}
│   │
│   ├── agent_analytics/
│   │   └── app/{worker.py, youtube_analytics_client.py, aggregation.py}
│   │
│   └── dashboard/                    # phase 2 — thin FastAPI+HTMX admin UI (or served by orchestrator/api)
│
├── libs/                             # shared internal packages, installed editable into every service image
│   ├── core/
│   │   ├── config.py                 # Pydantic Settings base classes
│   │   ├── logging.py                # structlog setup, shared JSON formatter
│   │   ├── db.py                     # SQLAlchemy async engine/session factory
│   │   └── celery_app.py             # shared Celery app factory + task base class (retry/backoff defaults)
│   │
│   ├── providers/                    # the swappable AI-provider abstraction layer
│   │   ├── llm/{base.py, anthropic_provider.py, openai_provider.py, registry.py}
│   │   ├── tts/{base.py, elevenlabs_provider.py, azure_provider.py, registry.py}
│   │   ├── image_gen/{base.py, stability_provider.py, openai_image_provider.py, registry.py}
│   │   ├── video_gen/{base.py, runway_provider.py, registry.py}
│   │   ├── stock_media/{base.py, pexels_provider.py, pixabay_provider.py, registry.py}
│   │   └── youtube/{data_api_client.py, analytics_api_client.py}
│   │
│   ├── storage/                      # media storage abstraction (local volume / MinIO / S3)
│   │   └── {base.py, minio_backend.py, local_backend.py}
│   │
│   ├── models/                       # SQLAlchemy ORM models, shared across all services
│   └── schemas/                      # Pydantic DTOs shared between orchestrator and agents (job payloads/results)
│
├── migrations/                       # Alembic migration scripts (single source of truth for schema)
│   ├── env.py
│   └── versions/
│
├── infra/
│   ├── caddy/Caddyfile
│   ├── monitoring/                   # phase 2: prometheus.yml, grafana dashboards/provisioning
│   └── backup/                       # pg_dump + MinIO sync scripts, run via a cron/backup Compose service
│
├── scripts/                          # operational one-offs: deploy.sh, restore.sh, seed_channel.py
│
├── tests/
│   ├── unit/                         # mirrors services/ and libs/ structure
│   ├── integration/                  # testcontainers-backed: real Postgres/Redis, mocked external HTTP
│   └── e2e/                          # full pipeline run against stubbed providers
│
└── data/                             # gitignored — local dev bind-mount for media output
```

## Rationale

- **One folder per agent under `services/`**, each with its own `Dockerfile` and
  dependency file. This means the Video Assembly agent can depend on
  `ffmpeg`/`moviepy`/large native libraries without bloating the image of the
  lightweight Research agent, and a dependency bump in one agent can't break
  another's build.
- **`libs/` is the only place cross-service code lives.** Every service installs
  it as an editable local dependency at build time. This is what makes the
  provider-swap requirement real: an agent never imports `openai` or
  `elevenlabs` directly, it imports `libs.providers.llm.registry.get_llm_provider()`.
- **`libs/schemas/` defines job payload/result contracts.** Since agents
  communicate only via queue messages and DB rows, these Pydantic models are the
  actual "API" between the Orchestrator and each agent — versioned and tested
  like one.
- **Migrations live in one top-level `migrations/` folder**, not per-service,
  because there is one shared database. This avoids migration-ordering
  conflicts that come from splitting migrations across services.
- **`tests/` mirrors the source tree** so it's obvious where a new agent's tests
  belong; integration tests use `testcontainers` to spin up real Postgres/Redis
  rather than mocking the database layer.
