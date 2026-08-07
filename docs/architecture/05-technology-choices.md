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

Adding a real provider is: write the class, register it in the YAML
under that capability, flip `active:` to its name (or set the
`{CAPABILITY}_PROVIDER` environment variable, e.g.
`VIDEO_GEN_PROVIDER=runway`, to override `active:` per-environment
without editing the file at all — `libs/providers/registry.py`). Four
capabilities default to a **local, self-hosted** implementation needing
no API key or paid vendor account at all — `llm` (Ollama), `tts`
(Kokoro), `video_gen`/`image_gen` (ComfyUI) — each with a cloud-vendor
alternative registered alongside it for when that's preferred instead;
`editor` (ffmpeg) has always been local-only, no stub. Every reasoning
call site in this codebase (Research, Script, Manager, Thumbnail,
Quality Control, Publisher) routes through the same swappable
`libs.providers.get_provider("llm")` capability — none of them import a
vendor SDK directly.

**Readiness (`Provider.check_readiness()`).** A provider being
*implemented* is not the same as its backend being *able*, and callers
that need to plan around a capability have to be able to tell the
difference. `check_readiness()` returns a `ProviderReadiness(ready,
reason)`; it defaults to ready, so implementing it stays optional for a
provider with nothing cheap to probe, and it never raises — an
unreachable backend is a legitimate "no", not an error the caller must
catch. Stub providers (`StubProvider`) always answer not-ready, from the
same `unavailable_reason` string their `NotImplementedError` carries.
Both ComfyUI providers answer it for real, by comparing the node classes
their configured workflow template references against the running
instance's `/object_info`.

That check exists because of a concrete failure: `video_gen`'s shipped
template needs AnimateDiff-Evolved custom nodes (see the `video_gen` row
below), a stock ComfyUI does not have them, and ComfyUI only reports
that as an HTTP 400 at submit time. `scripts/run_pipeline.py` turns
capability readiness into a hard constraint in the Script Agent's prompt
listing which `asset_requirements` types may be requested — so
mis-answering it does not degrade a video, it writes an entire script
around assets that can never be produced and fails the run at
`video_creation`. Approximating readiness as "the class name doesn't
start with `Stub`" is exactly what produced that outcome.

| Capability | Local default | Cloud alternative | Notes |
|---|---|---|---|
| LLM (ideation, scripting, quality control, publish metadata) | Ollama (`qwen3:32b` or any pulled model) | Anthropic Claude API, OpenAI GPT | `libs/providers/llm/ollama_provider.py` talks to a local Ollama server's `/api/chat`, forcing structured output via the `format` request field (grammar-constrained JSON-schema decoding) rather than tool-calling — the same `LLMToolCall.input_schema` every caller already builds for Anthropic works unchanged, no per-provider schema translation. Every reasoning call site (Research's `IdeaGenerator`/`KnowledgeBuilder`, the Script Agent's `ScriptGenerator`/`ScriptReviewer`, the Manager's reasoning engine, the Thumbnail Agent's `ThumbnailConceptGenerator`, the Publisher's `MetadataGenerator`, the Quality Control Agent's reviewers) goes through `libs.providers.get_provider("llm")` — `config/providers.yaml` defaults `llm.active` to `ollama`. One genuine capability gap: Research's `KnowledgeBuilder` needs Anthropic's server-side `web_search`/`web_fetch` tools (`generate_tool_call(..., enable_web_research=True)`) for actually-verified facts/citations — no local model here has an equivalent, so a knowledge package built under Ollama is honestly caveated in `supporting_notes` as reflecting the model's own knowledge rather than live-verified sources, instead of silently presenting unverified claims as researched ones. |
| Text-to-speech | Kokoro-82M (local, via the `kokoro` pip package's `KPipeline`) | ElevenLabs, Azure Speech | `libs/providers/tts/kokoro_provider.py` runs entirely on-device — no network call — encoding Kokoro's raw audio chunks to MP3 via the same `ffmpeg` binary the compositor already depends on. Kokoro's public API reports audio per text chunk, not per word, so `SynthesisResult.word_timings` is always `None` here; Voice Generation/Subtitle Generation's existing even-split-of-known-duration fallback covers this exactly the way it already does for Azure Speech's stub. ElevenLabs (`elevenlabs_provider.py`) remains a real, working cloud adapter, converting its per-*character* alignment into per-*word* timing when a paid vendor is preferred over local synthesis. |
| Image generation (thumbnails, static visuals) | ComfyUI (local) | Stability AI, OpenAI (DALL-E) | `libs/providers/image_gen/comfyui_provider.py` and `video_gen/comfyui_provider.py` share one HTTP client (`libs/providers/_comfyui_common.py`) against a local ComfyUI instance's REST API (`/prompt`, `/history`, `/view`) — no vendor account. Neither hardcodes a workflow: each loads a small JSON template from `config/comfyui/` (a real ComfyUI "Save (API Format)" graph, wrapped with an `output_node_title` and `{{PLACEHOLDER}}` tokens for prompt/dimensions/seed — see that directory's README) and fills in the caller's values before submitting, preserving each placeholder's real type (an `int` for width/seed, not a stringified one) since ComfyUI validates node inputs by type. Used by Asset Generation for storyboard shots and by the Thumbnail Agent for rendered thumbnail concepts — both call `get_provider("image_gen")` independently. |
| Video generation (AI b-roll) | ComfyUI (local) | Runway ML, InVideo AI, Google Veo | Same shared ComfyUI client as image generation above, targeting `config/comfyui/text_to_video.json`. Unlike the image template (core ComfyUI nodes only, stable), the video template is explicitly a starting-point example (AnimateDiff-Evolved + VideoHelperSuite's `VHS_VideoCombine`, the most commonly documented community text-to-video combo) rather than a verified graph — see `config/comfyui/README.md`: real text-to-video generation depends entirely on which custom nodes/checkpoint a given ComfyUI install actually has, so this is meant to be replaced with the user's own exported workflow. Runway (`runway_provider.py`) remains a real, working cloud adapter for when a paid vendor is preferred over local generation; InVideo AI and Google Veo remain honest stubs (no verifiable public API contract / no real GCP credentials to test against). MVP still relies primarily on stock footage to control cost and latency; see [Roadmap](./06-roadmap.md). |
| Stock footage/images (`stock_media`) | Pexels API | Pixabay API | Used by the Video Agent's Asset Generation module as the default visual source before AI generation — see [Agent Responsibilities §3.4](./03-agent-responsibilities.md#34-video-agent). |
| Sound effects / background music (`audio_library`) | Freesound API | Epidemic Sound | Also used by Asset Generation, for the two audio-only asset requirement types (`sound_effect`, `background_music_cue`) a script segment can declare. |
| Video rendering/composition | FFmpeg, invoked directly via `subprocess` (no `ffmpeg-python`/MoviePy wrapper) | Remotion | Chosen over Remotion to avoid adding a Node.js runtime dependency alongside the Python stack; Remotion remains a documented alternative if programmatic, React-authored templates are wanted later. Direct `subprocess` calls (`libs/providers/editor/ffmpeg_provider.py`) were chosen over a fluent Python wrapper for precise control over the filter graph and one less library surface to debug. Unlike every other capability in this table, this one needs no vendor account — ffmpeg is a free local binary (installed in the Video Agent's Dockerfile) — so it's a real, working implementation, not a stub. It composites video clips, generated images, narration, an optional whole-video background-music bed, and burned-in subtitles into one 1080p MP4: every visual normalized to a named `RenderProfile`'s resolution/fps (`config/render_profiles.yaml` — new formats/aspect ratios are a config entry, not a code change), EBU R128 loudness-normalized in a single pass, with per-stage progress reporting and a typed `EditorInputError`/`EditorTimeoutError`/`EditorRenderError` hierarchy so callers can distinguish bad input from a compositor bug from a stuck subprocess. Subtitle/overlay text is passed to ffmpeg's `drawtext` filter via `textfile=` (a small temp file) rather than an inline `text='...'` literal, since arbitrary narration text (apostrophes, colons) breaks that literal's escaping once it sits inside a larger `-filter_complex` string. |
| Captions | TTS provider's word timestamps, else an even split of the segment's known duration across its words | — | The Video Agent's Subtitle Generation module never requires provider-level timestamps — a provider that doesn't return them just gets a less precise (but still correct) even-split fallback instead of failing. ASR-based alignment (e.g. Whisper) instead of the even-split fallback is a possible future improvement, not built yet. |
| Publishing & analytics | YouTube Data API v3 + YouTube Analytics API | — | Not swappable (there's only one YouTube), but isolated behind `libs/providers/youtube/` so OAuth logic and the resumable-upload protocol live in one place. `youtube_data_api_provider.py` is a real, working adapter — raw `requests` calls (no `google-api-python-client`, same choice as `runway_provider.py`), OAuth2 refresh-token exchange with in-memory-only access tokens, genuine resume-from-offset on an interrupted upload, and a typed error hierarchy distinguishing auth failures from quota exhaustion (`YouTubeQuotaError` — a "come back later" signal, not retried in-process) from every other upload failure. `youtube.active` stays `stub` until real OAuth credentials are configured, the same convention as `video_gen`/`tts` (unlike `llm`/`editor`, which default to their real implementation) — publishing to a real channel is a consequential, public, hard-to-reverse action. |

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
