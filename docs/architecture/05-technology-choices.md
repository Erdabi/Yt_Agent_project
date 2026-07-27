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

Most capabilities are still only the `stub` implementation today —
enough to prove the switching mechanism (YAML → dynamic import →
instantiated class) is real and testable without fabricating an actual
vendor integration. Adding a real provider is: write the class, register
it in the YAML under that capability, flip `active:` to its name (or set
the `{CAPABILITY}_PROVIDER` environment variable, e.g.
`VIDEO_GEN_PROVIDER=runway`, to override `active:` per-environment
without editing the file at all — `libs/providers/registry.py`). Three
capabilities already have a real implementation alongside their stub:
`video_gen` (Runway), `tts` (ElevenLabs) — both requiring a real API key
to actually use — and `editor` (ffmpeg, requiring none) — see the table
below.

| Capability | Primary choice | Fallback | Notes |
|---|---|---|---|
| LLM (ideation, scripting, QA policy review) | Anthropic Claude API | OpenAI GPT | Used for scoring, script generation, fact-checking, and policy review — all through one `LLMProvider` interface so prompt logic is provider-agnostic. |
| Text-to-speech | ElevenLabs | Azure Speech | ElevenLabs (`libs/providers/tts/elevenlabs_provider.py`) is a real, working adapter against ElevenLabs' public "create speech with timing" endpoint (auth, request creation, retryable-vs-permanent error handling — 429/5xx retried with backoff, 4xx fails fast — all internal to that one file); `tts.active` stays `stub` until a real `ELEVENLABS_API_KEY` is configured. It converts ElevenLabs' per-*character* alignment into the per-*word* timing `SynthesisResult` promises, which drives caption sync when available — Subtitle Generation falls back to an even split of the segment's known duration otherwise. Azure Speech (`azure_speech_provider.py`) is an honest stub rather than a fabricated integration: its REST API needs a resource-specific OAuth token exchange this codebase has no real Azure resource to verify against. |
| Image generation (thumbnails, static visuals) | Stability AI | OpenAI (DALL-E) | Used by Asset Generation for storyboard shots and by the Thumbnail Agent (`services/agent_video/app/thumbnail_agent.py`) for rendered thumbnail concepts — both call `get_provider("image_gen")` independently, so this is the same swappable capability either way. |
| Video generation (AI b-roll) | Runway ML | InVideo AI, Google Veo | Runway (`libs/providers/video_gen/runway_provider.py`) is a real, working adapter against Runway's public developer API (auth, request creation, polling, download, and Runway-specific error handling — all internal to that one file); `video_gen.active` stays `stub` until a real `RUNWAY_API_KEY` is configured, so nothing calls a paid vendor by default. InVideo AI and Google Veo (`invideo_provider.py`/`veo_provider.py`) are honest stubs rather than fabricated integrations: InVideo publishes no verifiable public REST API contract as of this writing, and Veo needs Vertex AI OAuth service-account credentials this codebase has no way to test against — both raise a clear error explaining exactly what's missing, the same as every other unimplemented vendor in this table. MVP still relies primarily on stock footage to control cost and latency; see [Roadmap](./06-roadmap.md). |
| Stock footage/images (`stock_media`) | Pexels API | Pixabay API | Used by the Video Agent's Asset Generation module as the default visual source before AI generation — see [Agent Responsibilities §3.4](./03-agent-responsibilities.md#34-video-agent). |
| Sound effects / background music (`audio_library`) | Freesound API | Epidemic Sound | Also used by Asset Generation, for the two audio-only asset requirement types (`sound_effect`, `background_music_cue`) a script segment can declare. |
| Video rendering/composition | FFmpeg, invoked directly via `subprocess` (no `ffmpeg-python`/MoviePy wrapper) | Remotion | Chosen over Remotion to avoid adding a Node.js runtime dependency alongside the Python stack; Remotion remains a documented alternative if programmatic, React-authored templates are wanted later. Direct `subprocess` calls (`libs/providers/editor/ffmpeg_provider.py`) were chosen over a fluent Python wrapper for precise control over the filter graph and one less library surface to debug. Unlike every other capability in this table, this one needs no vendor account — ffmpeg is a free local binary (installed in the Video Agent's Dockerfile) — so it's a real, working implementation, not a stub. It composites video clips, generated images, narration, an optional whole-video background-music bed, and burned-in subtitles into one 1080p MP4: every visual normalized to a named `RenderProfile`'s resolution/fps (`config/render_profiles.yaml` — new formats/aspect ratios are a config entry, not a code change), EBU R128 loudness-normalized in a single pass, with per-stage progress reporting and a typed `EditorInputError`/`EditorTimeoutError`/`EditorRenderError` hierarchy so callers can distinguish bad input from a compositor bug from a stuck subprocess. Subtitle/overlay text is passed to ffmpeg's `drawtext` filter via `textfile=` (a small temp file) rather than an inline `text='...'` literal, since arbitrary narration text (apostrophes, colons) breaks that literal's escaping once it sits inside a larger `-filter_complex` string. |
| Captions | TTS provider's word timestamps, else an even split of the segment's known duration across its words | — | The Video Agent's Subtitle Generation module never requires provider-level timestamps — a provider that doesn't return them just gets a less precise (but still correct) even-split fallback instead of failing. ASR-based alignment (e.g. Whisper) instead of the even-split fallback is a possible future improvement, not built yet. |
| Publishing & analytics | YouTube Data API v3 + YouTube Analytics API | — | Not swappable (there's only one YouTube), but isolated behind `libs/providers/youtube/` so quota handling and OAuth logic live in one place. |

Retry/backoff on transient errors is currently each adapter's own
responsibility rather than a shared `libs/providers` wrapper — e.g.
`RunwayProvider` retries a dropped connection or a 5xx response with
exponential backoff, but fails immediately (no retry) on a 4xx or a
provider-reported terminal failure, since those won't be fixed by
retrying the identical request. A shared retry/circuit-breaker layer
that fails over to the next-priority `provider_configs` entry after N
consecutive failures, logged as a `system_events` row, remains a
documented future enhancement, not built yet.

## 5.3 Prompt management (the versioned-template layer)

Every prompt sent to an LLM is a file under `prompts/` at the repo root
(`prompts/<agent>/<name>/v<N>[.<provider>].yaml`), loaded through
`libs/prompts` (`get_prompt_loader()`) rather than embedded as a Python
string constant — the same "config, not code" philosophy as §5.2's
provider registry, applied to wording instead of vendor selection.

- **Versioning**: `version="latest"` resolves to the highest `vN` present
  on disk; pinning an older version (e.g. `MANAGER_PROMPT_VERSION=v1`) is
  a config change, not a rollback commit.
- **Variables**: templates are rendered with Jinja2
  (`{{ variable }}`, `{% if %}`, `{% for %}`, filters) using
  `StrictUndefined`, so a caller that forgets a variable gets an
  immediate, readable error instead of an LLM silently receiving "None"
  in its prompt.
- **Provider-specific overrides**: `v1.claude.yaml` next to `v1.yaml`
  gets picked when the caller requests `provider="claude"`; requesting a
  provider with no override file is not an error, it just falls back to
  the default wording. This is what "different prompts for Claude vs.
  other LLMs" means in practice — one example exists today
  (`prompts/manager/workflow_decision_system/`), tuned around Claude's
  forced-tool-choice behavior.

The Manager Agent's reasoning engine (§3.1) is the one real integration
today, selecting its prompt templates at runtime rather than using a
hardcoded string. Research/Script/Video/QA each have a draft `v1`
template ready (`prompts/research/`, `prompts/script/`, `prompts/video/`,
`prompts/qa/`) for when their real LLM calls land — see
[Roadmap](./06-roadmap.md) — the same "interface before implementation"
approach `libs/providers`' stub providers already use. See
`prompts/README.md` for the full file-naming convention.

## 5.4 Logging, error handling, observability

| Concern | Choice | Why |
|---|---|---|
| Structured logging | `structlog`, JSON to stdout | Docker's logging driver captures stdout; JSON format is ready to ship to a log aggregator later without changing application code. |
| Error tracking | Sentry | Captures exceptions with structured context (project id, stage, provider, attempt number) across every service. |
| Alerting | Telegram/Slack webhook from the Orchestrator | Fired when a job exhausts its retry budget or a project lands in `needs_human_review` — the operator shouldn't have to poll the dashboard to notice a stuck pipeline. |
| Celery monitoring | Flower | Queue depth, task history, worker health — low-effort visibility into the queue layer specifically. |
| Metrics/dashboards (phase 2) | Prometheus + Grafana | Added once there's more than one operator or the system runs unattended for long stretches; not needed for the MVP. |

## 5.5 Configuration management

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

## 5.6 Testing & CI/CD

| Concern | Choice | Why |
|---|---|---|
| Test framework | pytest + pytest-asyncio | Standard for the async FastAPI/Celery stack. |
| External API mocking | `respx` (HTTP-level) / `vcrpy` (record-replay) | Tests run without hitting real LLM/TTS/YouTube quotas or incurring cost. |
| Integration tests | `testcontainers` (real Postgres + Redis in Docker) | Confidence that migrations and queries work against the real engine, not a mock. |
| Dependency management | `uv` (or Poetry) per service, lockfiles committed | Reproducible builds; per-service lockfiles keep the heavy video-assembly dependencies out of lightweight agents' images. |
| CI | GitHub Actions | Lint (ruff), type-check (mypy), test, build each service's Docker image on PR; build+push to GHCR on merge to main. |
| Deployment | SSH + `docker compose pull && docker compose up -d` (scripted in `scripts/deploy.sh`), or Watchtower for auto-pull | Matches the single-VPS, Compose-based deployment target — no need for a full CD platform at this scale. |

## 5.7 Why not more, sooner

Kubernetes, a message bus like Kafka, and a dedicated observability stack
(Prometheus/Grafana/Loki) are all reasonable choices for a larger deployment,
but are deliberately deferred: this system runs on one VPS, with a handful of
agents and a moderate job volume (a few videos a day, not per second). Celery
+ Redis + Compose covers the actual throughput needs; adding heavier
infrastructure now would mean more to operate without a corresponding
reliability or performance benefit. The roadmap ([§6](./06-roadmap.md), Phase 5)
revisits this once real usage data justifies it.
