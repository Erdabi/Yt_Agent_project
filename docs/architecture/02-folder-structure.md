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
├── config/
│   ├── providers.yaml                 # which concrete class backs each swappable capability (libs/providers)
│   └── render_profiles.yaml           # named output-format bundles (resolution/fps/loudness/crossfade/subtitle-size) for libs/providers/editor
│
├── prompts/                          # versioned prompt template files, loaded via libs/prompts —
│   │                                 # never embedded as Python string constants — see prompts/README.md
│   ├── manager/{workflow_decision_system/, workflow_decision_user/}
│   ├── research/{generate_ideas_system/, generate_ideas_user/,
│   │             build_knowledge_package_system/, build_knowledge_package_user/}
│   ├── script/{generate_script_system/, generate_script_user/,
│   │           review_script_system/, review_script_user/}
│   ├── video/storyboard_shot_planning/
│   ├── thumbnail/{generate_concepts_system/, generate_concepts_user/}
│   ├── qa/{script_review_system/, script_review_user/,
│   │       video_review_system/, video_review_user/,
│   │       thumbnail_review_system/, thumbnail_review_user/,
│   │       policy_review/}                        # policy_review/ is still an unwired draft
│   └── publish/{refine_metadata_system/, refine_metadata_user/}
│
├── services/                         # one folder per deployable container
│   ├── orchestrator/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── app/
│   │       ├── main.py               # FastAPI app (internal API + dashboard backend)
│   │       ├── manager/              # the Manager Agent: workflow plan, dispatcher, Claude
│   │       │                         # reasoning engine, state transitions — see 03-agent-responsibilities.md §3.1
│   │       ├── approval_gates.py     # config-driven human-in-the-loop rules (phase 2)
│   │       └── api/                  # routers: projects, ideas, jobs, goals, approvals
│   │
│   ├── agent_research/                # implemented — see 03-agent-responsibilities.md §3.2
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── worker.py             # ResearchAgent (enrich one goal / discover several), Celery tasks
│   │       ├── idea_generator.py     # picks/scores a topic (forced tool use, prompts from prompts/research/)
│   │       ├── knowledge_builder.py  # deep-researches a chosen topic (web_search/web_fetch, tool_choice NOT forced)
│   │       ├── knowledge_package_render.py  # renders a KnowledgePackage as Markdown
│   │       ├── dedup.py              # lexical duplicate-content check (title + keyword overlap)
│   │       └── trend_sources/        # base.py, seed_list.py (real), youtube_trending.py,
│   │                                 # google_trends.py, reddit.py, rss.py (stubs), aggregator.py
│   │
│   ├── agent_scriptwriter/            # implemented — see 03-agent-responsibilities.md §3.3
│   │   ├── Dockerfile
│   │   └── app/
│   │       ├── worker.py             # ScriptwriterAgent: builds a ProjectContext, persists Script + ScriptSegments
│   │       ├── script_generator.py   # drafts hook/intro/main sections/ending/CTA (forced tool use, prompts from prompts/script/)
│   │       ├── script_reviewer.py    # lightweight self-review pass: factual consistency, retention, repetition
│   │       └── script_schema.py      # Claude tool-schema + GeneratedScript dataclasses (imports shared types
│   │                                 # from libs/schemas/script_production.py — see that entry below)
│   │
│   ├── agent_video/                  # one agent, seven pipeline modules, plus the Thumbnail Agent
│   │   │                             # invoked in-process — see 03-agent-responsibilities.md §3.4
│   │   └── app/
│   │       ├── worker.py             # Celery task entrypoints: agents.video.run + agents.thumbnail.run (queue: video)
│   │       ├── video_agent.py        # VideoAgent.run() calls the seven modules below in sequence, then the Thumbnail Agent
│   │       ├── visual_prompt.py       # composes a cinematic generation prompt per visual beat (framing/lighting/medium/quality)
│   │       ├── thumbnail_agent.py     # ThumbnailAgent(BaseAgent) — a genuine, independent agent (see below)
│   │       ├── thumbnail_concept_generator.py  # Claude forced tool-use call: topic/title/script/branding -> ranked concepts
│   │       ├── pipeline_schema.py     # typed input/output contracts shared by every module below,
│   │       │                         # including ASSET_TYPE_ROUTING (script AssetType -> provider capability)
│   │       ├── asset_cache.py         # AssetCache/AssetCacheKey: dedup layer in front of every provider call
│   │       │                         # below (and thumbnail_agent.py's) — checks for an identical prior
│   │       │                         # request before calling a provider
│   │       └── modules/
│   │           ├── visual_beat_planning.py # splits each segment's narration into 3-6s visual beats from real measured audio — no provider calls
│   │           ├── asset_planning.py      # routes each beat + segment-wide requirements to capabilities — no provider calls
│   │           ├── asset_generation.py    # checks AssetCache, else calls image_gen/video_gen/stock_media/audio_library, persists Asset+StoryboardShot
│   │           ├── voice_generation.py    # checks AssetCache, else calls tts, persists Asset+Voiceover
│   │           ├── subtitle_generation.py # caption cues from real or estimated word timing — no provider calls
│   │           ├── timeline_building.py   # cumulative absolute timing + final assembly — no provider calls
│   │           └── rendering.py           # builds a render plan, then calls the editor provider to composite it
│   │
│   ├── agent_qa/                      # implemented — see 03-agent-responsibilities.md §3.8
│   │   └── app/
│   │       ├── worker.py             # Celery task entrypoint (queue: qa)
│   │       ├── quality_control_agent.py  # QualityControlAgent: gathers ReviewInput once, runs every
│   │       │                             # reviewer, aggregates APPROVED/REJECTED, persists qa_reports
│   │       ├── qa_schema.py           # Issue/ReviewResult/ReviewInput + gathered production-data types
│   │       ├── media_inspection.py    # shared ffprobe/ffmpeg helpers (probe/decode/silence/black-frame/peak-level)
│   │       ├── llm_review.py          # shared "report issues" tool + get_provider("llm") + track_llm_call wrapper
│   │       └── reviewers/
│   │           ├── base.py               # the Reviewer interface every reviewer implements
│   │           ├── script_reviewer.py    # completeness/repetition (deterministic) + factual/quality/engagement/grammar (LLM)
│   │           ├── video_reviewer.py     # assets/order/rendering/duration (deterministic) + visual consistency/transitions (LLM)
│   │           ├── audio_reviewer.py     # narration/clipping/silence/timing/sync — deterministic only
│   │           ├── subtitle_reviewer.py  # timing/readability/overlap/missing captions — deterministic only
│   │           └── thumbnail_reviewer.py # readability/title visibility/branding/click potential — vision LLM call
│   │
│   ├── agent_publisher/               # implemented — see 03-agent-responsibilities.md §3.9
│   │   └── app/
│   │       ├── worker.py             # Celery task entrypoint (queue: publish)
│   │       ├── publisher_agent.py    # PublisherAgent: gathers script/render/thumbnail, refines
│   │       │                         # metadata, uploads+thumbnail+playlist+verify, persists publications
│   │       └── metadata_generator.py # MetadataGenerator: get_provider("llm") forced tool call ->
│   │                                 # refined title/description/tags/suggested_playlist_theme
│   │
│   ├── agent_analytics/              # NOT dispatched by the Manager — see 03-agent-responsibilities.md §3.10
│   │   └── app/
│   │       ├── worker.py             # AnalyticsAgent + the sweep task + its own beat_schedule
│   │       ├── youtube_analytics_client.py
│   │       └── aggregation.py
│   │
│   └── dashboard/                    # phase 2 — thin FastAPI+HTMX admin UI (or served by orchestrator/api)
│
├── libs/                             # shared internal packages, installed editable into every service image
│   ├── core/
│   │   ├── config.py                 # Pydantic Settings base classes
│   │   ├── logging.py                # structlog setup, shared JSON formatter
│   │   ├── db.py                     # SQLAlchemy async engine/session factory
│   │   └── celery_app.py             # shared Celery app factory + task base class
│   │
│   ├── agents/
│   │   └── base.py                   # BaseAgent: execute_job, Manager notification, reports_to_manager flag
│   │
│   ├── providers/                    # the swappable AI-provider abstraction layer, config-file-driven
│   │   ├── base.py                   # Provider marker + ProviderConfigError
│   │   ├── registry.py               # get_provider(capability) -> instance, reads config/providers.yaml
│   │   ├── video_gen/{base.py, stub_provider.py, runway_provider.py, invideo_provider.py, veo_provider.py}  # runway_provider.py is real; the other two are honest stubs (no verifiable public API)
│   │   ├── tts/{base.py, stub_provider.py, elevenlabs_provider.py, azure_speech_provider.py}  # elevenlabs_provider.py is real; azure_speech_provider.py is an honest stub
│   │   ├── image_gen/{base.py, stub_provider.py}
│   │   ├── stock_media/{base.py, stub_provider.py}    # StockMediaProvider.search() — stock footage/photos
│   │   ├── audio_library/{base.py, stub_provider.py}  # AudioLibraryProvider.search() — sound effects/music cues
│   │   ├── editor/{base.py, stub_provider.py, ffmpeg_provider.py, profiles.py}  # compositor — ffmpeg_provider.py is real, not a stub (no vendor account needed); profiles.py loads config/render_profiles.yaml (resolution/fps/loudness/crossfade/subtitle-size bundles, one per output format)
│   │   ├── llm/{base.py, stub_provider.py, anthropic_provider.py}  # forced-tool-use "generate_tool_call" — anthropic_provider.py is real and the default (active: anthropic, not stub — see its own docstring for why); first real consumer is the Quality Control Agent's reviewers (services/agent_qa/app/reviewers/)
│   │   └── youtube/{base.py, stub_provider.py, youtube_data_api_provider.py}  # youtube_data_api_provider.py is real (OAuth2 refresh-token flow + resumable upload against the Data API v3); active stays stub until real credentials are configured
│   │
│   ├── storage/                      # centralized asset storage, keyed by project id
│   │   ├── base.py                   # StorageBackend interface
│   │   ├── local_backend.py          # the only backend that exists today
│   │   └── registry.py               # get_storage_backend(), reads STORAGE_BACKEND/STORAGE_ROOT
│   │
│   ├── prompts/                      # Prompt Management System — loads prompts/ (above), never a
│   │   │                             # Python string constant in agent code
│   │   ├── loader.py                 # PromptLoader/PromptTemplate: versioning + variables + provider overrides
│   │   └── registry.py               # get_prompt_loader(), reads PROMPTS_ROOT
│   │
│   ├── context/                      # Project Context Builder — gathers Channel/Project/Research/
│   │   │                             # Knowledge Package/prompt version/Manager settings into one
│   │   │                             # immutable object, instead of a consumer querying each itself
│   │   ├── schema.py                 # ProjectContext and its frozen sub-models
│   │   └── builder.py                # build_project_context(project_id) — real DB+storage read, no writes
│   │
│   ├── llm_usage/                    # usage tracking for every LLM call — provider, model, prompt
│   │   │                             # version, tokens, elapsed time, estimated cost
│   │   ├── tracker.py                 # track_llm_call() context manager + record_llm_usage()
│   │   └── pricing.py                 # per-model $/1M-token table + estimate_cost_usd()
│   │
│   ├── models/                       # SQLAlchemy ORM models, shared across all services
│   └── schemas/                      # Pydantic DTOs shared between orchestrator and agents
│       ├── jobs.py                   # JobContext — job payloads/results
│       ├── knowledge.py              # KnowledgePackage — the Research Agent's output, Script Agent's input
│       └── script_production.py      # SegmentProductionMetadata/AssetRequirement/AssetType — the Script
│                                     # Agent's output, the Video Agent's Asset Planning module's input
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
  provider-swap requirement real: an agent never imports `runwayml` or
  `elevenlabs` directly, it calls `libs.providers.registry.get_provider("tts")`,
  which resolves the active implementation from `config/providers.yaml` —
  one shared registry function generic over capability, rather than a
  separate `registry.py` duplicated under every provider subfolder.
- **`libs/schemas/` defines job payload/result contracts.** Since agents
  communicate only via queue messages and DB rows, these Pydantic models are the
  actual "API" between the Orchestrator and each agent — versioned and tested
  like one. This is also why `script_production.py`'s types live here rather
  than inside `services/agent_scriptwriter/`: a service's own `app/` package is
  never imported by another service (only `libs/` is shared), and the Video
  Agent's Asset Planning module needs to parse `ScriptSegment.production_metadata`
  with the same types the Script Agent validated it against.
- **Prompts are versioned files under `prompts/`, not Python string constants.**
  Every agent that calls an LLM loads its prompt through
  `libs.prompts.get_prompt_loader()` rather than embedding the wording in its
  own module — this is what makes a prompt tunable, versionable
  (`prompts/<agent>/<name>/v2.yaml`), and providable per-LLM
  (`v1.claude.yaml`) without a code change. See
  [Agent Responsibilities §3.1](./03-agent-responsibilities.md) for the one
  place this is wired into real code today (the Manager's reasoning engine).
- **`libs/context/` gathers, `libs/llm_usage/` measures.** A downstream agent
  (the Script Agent first) calls `build_project_context(project_id)` once and
  gets back a single immutable snapshot instead of separately querying
  Channel/Project/VideoIdea/storage/settings itself; every Claude call in the
  codebase wraps its API call in `libs.llm_usage.track_llm_call(...)`, which
  always records one `LLMUsageLog` row — success or failure — so spend is
  attributable by project/agent/model without depending on any agent
  remembering to log it manually.
- **Migrations live in one top-level `migrations/` folder**, not per-service,
  because there is one shared database. This avoids migration-ordering
  conflicts that come from splitting migrations across services.
- **`tests/` mirrors the source tree** so it's obvious where a new agent's tests
  belong; integration tests use `testcontainers` to spin up real Postgres/Redis
  rather than mocking the database layer.
