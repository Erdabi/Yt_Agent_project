# 5. Technology Choices

## 5.1 Core stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Every required AI SDK (Anthropic, OpenAI, ElevenLabs, YouTube APIs) has first-class Python support; one language across all agents keeps `libs/` genuinely shared. |
| Web/API framework | FastAPI | Async-native, Pydantic v2 request/response validation, automatic OpenAPI docs for the internal dashboard API. |
| Task queue | Celery + Redis | Mature retry/backoff/rate-limiting and a scheduler (`beat`) in one package; large ecosystem for monitoring (Flower). Lighter alternative considered: **Arq** (asyncio-native, simpler) — worth revisiting if Celery's overhead becomes a pain point, but Celery's maturity wins for a first production build. |
| Database | PostgreSQL 16 | Strong relational integrity for the pipeline state machine, JSONB for flexible provider metadata, `pgvector` extension for idea-dedup embeddings — one database serves all of it. |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | Async matches FastAPI/Celery-async workers; Alembic is the de facto standard for versioned schema changes. |
| Cache / broker | Redis 7 | Doubles as Celery broker, result backend, and a place for provider rate-limit counters/locks — one moving part instead of three. |
| Object storage | MinIO (S3-compatible), Docker volume-backed | Gives an S3 interface from day one on a single VPS; migrating to Hetzner Object Storage or AWS S3 later is a config change (endpoint + credentials), not a rewrite of every agent that writes media. |
| Reverse proxy / TLS | Caddy | Automatic HTTPS with zero manual certificate management for the admin dashboard; simpler than Traefik for a single-host deployment with a handful of routes. |
| Containerization | Docker Compose | Explicitly required; one host, one stack. Documented upgrade path to Nomad/Kubernetes only if the system ever outgrows a single VPS — not needed at this scale. |

## 5.2 AI provider integrations (the swappable layer)

Every capability below is accessed through an internal interface in
`libs/providers/<capability>/base.py`; concrete providers implement that
interface, and `libs/providers/registry.py` selects which one is active by
reading `config/providers.yaml` — one file, one `active:` line per
capability, plus the non-secret parameters and `secret_ref` for each
registered provider (path configurable via `PROVIDERS_CONFIG_PATH`). This
is the process-wide default; `provider_configs` (database-backed, see
[Database Design §4.2](./04-database-design.md#core-tables)) can override
it per channel by name, plus track priority order and usage/cost logging.
Swapping providers — or adding a fallback chain — is a config file (or
database) change, never a code change in an agent.

Only the `stub` implementation is registered for each capability today —
enough to prove the switching mechanism (YAML → dynamic import →
instantiated class) is real and testable without fabricating an actual
vendor integration. Adding a real provider is: write the class, register
it in the YAML under that capability, flip `active:` to its name.

| Capability | Primary choice | Fallback | Notes |
|---|---|---|---|
| LLM (ideation, scripting, QA policy review) | Anthropic Claude API | OpenAI GPT | Used for scoring, script generation, fact-checking, and policy review — all through one `LLMProvider` interface so prompt logic is provider-agnostic. |
| Text-to-speech | ElevenLabs | Azure Speech / OpenAI TTS | Selected per-voice in `provider_configs.config`; word-level timestamps (when supported) drive caption sync. |
| Image generation (thumbnails, static visuals) | Stability AI | OpenAI (DALL-E) | |
| Video generation (AI b-roll) | Runway ML | Pika | Phase 2+ — MVP relies primarily on stock footage to control cost and latency; see [Roadmap](./06-roadmap.md). |
| Stock footage/images | Pexels API | Pixabay API | Used by the Storyboard Agent as the default visual source before AI generation. |
| Video rendering/composition | FFmpeg (+ a thin Python compositing layer, e.g. `ffmpeg-python`/MoviePy) | — | Chosen over Remotion to avoid adding a Node.js runtime dependency alongside the Python stack; Remotion remains a documented alternative if programmatic, React-authored templates are wanted later. |
| Captions/ASR | TTS provider's word timestamps, else Whisper | — | Falls back to ASR alignment if the active TTS provider doesn't return timestamps. |
| Publishing & analytics | YouTube Data API v3 + YouTube Analytics API | — | Not swappable (there's only one YouTube), but isolated behind `libs/providers/youtube/` so quota handling and OAuth logic live in one place. |

Provider calls share one retry/backoff wrapper (`libs/providers` base class):
exponential backoff on transient errors, and a circuit breaker that fails over
to the next-priority provider after N consecutive failures — logged as a
`system_events` row so a provider outage is visible, not silent.

## 5.3 Logging, error handling, observability

| Concern | Choice | Why |
|---|---|---|
| Structured logging | `structlog`, JSON to stdout | Docker's logging driver captures stdout; JSON format is ready to ship to a log aggregator later without changing application code. |
| Error tracking | Sentry | Captures exceptions with structured context (project id, stage, provider, attempt number) across every service. |
| Alerting | Telegram/Slack webhook from the Orchestrator | Fired when a job exhausts its retry budget or a project lands in `needs_human_review` — the operator shouldn't have to poll the dashboard to notice a stuck pipeline. |
| Celery monitoring | Flower | Queue depth, task history, worker health — low-effort visibility into the queue layer specifically. |
| Metrics/dashboards (phase 2) | Prometheus + Grafana | Added once there's more than one operator or the system runs unattended for long stretches; not needed for the MVP. |

## 5.4 Configuration management

- **Pydantic Settings** (`pydantic-settings`) per service, all subclassing a
  shared base in `libs/core/config.py` — every setting is typed, validated at
  process startup (fail fast on a missing/malformed env var), and documented
  by the class itself.
- **`.env` file** (git-ignored) is the source of truth for secrets and
  per-environment values locally; `.env.example` in the repo documents every
  variable with a placeholder. Docker Compose `secrets:` is a documented
  upgrade path if the deployment ever needs tighter secret isolation than a
  mounted env file.
- **Which provider implementations exist, and the default active one per
  capability, lives in `config/providers.yaml`** (§5.2) — not env vars, so
  registering a new provider or changing the process-wide default doesn't
  require a schema change. **Per-channel overrides live in the database**
  (`provider_configs`) — this is what lets an operator swap a specific
  channel's active LLM/TTS/image provider from the admin dashboard without
  a redeploy. Env vars hold only the actual API keys (`secret_ref`, in
  both the YAML and the table, points at the var name, never the key
  itself).

## 5.5 Testing & CI/CD

| Concern | Choice | Why |
|---|---|---|
| Test framework | pytest + pytest-asyncio | Standard for the async FastAPI/Celery stack. |
| External API mocking | `respx` (HTTP-level) / `vcrpy` (record-replay) | Tests run without hitting real LLM/TTS/YouTube quotas or incurring cost. |
| Integration tests | `testcontainers` (real Postgres + Redis in Docker) | Confidence that migrations and queries work against the real engine, not a mock. |
| Dependency management | `uv` (or Poetry) per service, lockfiles committed | Reproducible builds; per-service lockfiles keep the heavy video-assembly dependencies out of lightweight agents' images. |
| CI | GitHub Actions | Lint (ruff), type-check (mypy), test, build each service's Docker image on PR; build+push to GHCR on merge to main. |
| Deployment | SSH + `docker compose pull && docker compose up -d` (scripted in `scripts/deploy.sh`), or Watchtower for auto-pull | Matches the single-VPS, Compose-based deployment target — no need for a full CD platform at this scale. |

## 5.6 Why not more, sooner

Kubernetes, a message bus like Kafka, and a dedicated observability stack
(Prometheus/Grafana/Loki) are all reasonable choices for a larger deployment,
but are deliberately deferred: this system runs on one VPS, with a handful of
agents and a moderate job volume (a few videos a day, not per second). Celery
+ Redis + Compose covers the actual throughput needs; adding heavier
infrastructure now would mean more to operate without a corresponding
reliability or performance benefit. The roadmap ([§6](./06-roadmap.md), Phase 5)
revisits this once real usage data justifies it.
