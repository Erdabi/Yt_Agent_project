# 6. Development Roadmap

Phased so that a working, human-supervised end-to-end pipeline exists as early
as possible (Phase 1), with automation and autonomy added incrementally on top
of a system that already produces a real video. Timeframes are rough sizing
for a single developer, not commitments.

## Phase 0 — Foundations (~1–2 weeks)

- Repo scaffolding per [Folder Structure](./02-folder-structure.md); base
  Docker images; Docker Compose skeleton with just `postgres`, `redis`,
  `minio` running.
- `libs/core` (config, logging, DB session/engine, Celery app factory).
- Provider abstraction interfaces in `libs/providers/*`, config-file-driven
  (`config/providers.yaml`) and backed by stub implementations only — this
  lets every later agent be built against a stable interface before real
  API keys/costs are involved. **Done** as of the Manager Agent /
  video-consolidation refactor: the switching mechanism (YAML → dynamic
  import → instantiated class) is real; only the vendor SDK calls
  themselves remain stubs.
- Centralized asset storage abstraction (`libs/storage`), local-filesystem
  backend only, keyed by project id. **Done** for the same reason above —
  a real media-producing agent has something to write to from day one.
- Prompt Management System (`libs/prompts`, `prompts/`): versioned
  template files with variables and provider-specific overrides, replacing
  Python string constants. **Done** — the Manager, Research, and Script
  agents' real Claude calls all load their prompts this way; Video/QA each
  still have a draft `v1` template waiting for the real LLM call that will
  load it.
- Alembic baseline migration for the core schema
  ([Database Design §4.2](./04-database-design.md#core-tables)).
- CI pipeline skeleton (lint, type-check, test, build images).

**Exit criteria:** `docker compose up` brings up a healthy empty stack;
`alembic upgrade head` runs clean; CI is green on an empty test suite.

## Phase 1 — MVP single-path pipeline (~3 weeks)

- Manager Agent with a simple linear state machine (no retry-routing
  sophistication yet). **Done**, including the Claude-backed reasoning
  engine and deterministic fallback.
- Research Agent: manual/seed topic list + one LLM call to expand into
  scored ideas (live trend scraping deferred to Phase 4). **Done** —
  trend analysis, topic generation, lexical duplicate-content checking,
  scoring, audience/angle/competition-level/length/notes output, both in
  Manager-dispatched (enrich one goal) and channel-level (discover
  several) modes, plus a structured Knowledge Package (verified facts,
  timeline, entities, citations, keywords, related topics, hooks,
  supporting notes — `libs/schemas/knowledge.py`) built with real
  web-search-backed research for the one enriched idea, stored as JSON +
  Markdown for the Script Agent to consume directly. Live trend scraping
  (YouTube Trending/Google Trends/Reddit/RSS) and embedding-based semantic
  dedup remain Phase 4, as originally planned; automatic scheduling for
  discover mode (a daily ideation run per channel) is unbuilt too — it's
  callable today but nothing calls it on a cadence yet.
- Script Agent with one real LLM provider. **Done** — one structured Claude
  call producing hook, introduction, a deliberately chosen story structure,
  main sections, retention techniques, ending, and call to action, each
  beat with voice-over text, a scene description, and visual suggestions;
  writes from the Research Agent's Knowledge Package when one exists;
  persisted as a versioned `scripts` row plus ordered `script_segments`.
- Video Agent's modules land one real implementation at a time, in-process
  (no separate agents/queues to coordinate — see
  [Agent Responsibilities §3.4](./03-agent-responsibilities.md#34-video-agent)).
  **Done end to end** — Asset Planning, Asset Generation, Voice
  Generation, Subtitle Generation, Timeline Building, Rendering, and
  Thumbnail Generation are seven real, independently-testable modules
  with typed inputs/outputs (services/agent_video/app/pipeline_schema.py),
  each calling `libs.providers` (`image_gen`/`video_gen`/`stock_media`/
  `audio_library`/`tts`/`editor`) only where it genuinely needs to, so
  swapping one capability's provider never touches another module's code
  — proven by running the full pipeline end to end against real
  Postgres, producing an actual playable MP4 and thumbnail image, and
  confirming the Asset Cache (below) makes a second, identical request
  reuse prior output with zero new provider calls. What's still a real
  *vendor* integration away: `image_gen`/`video_gen`/`stock_media`/
  `audio_library`/`tts` are all still the `stub` implementation
  (config/providers.yaml) — no AI-generated visuals or real narration
  yet, MVP relies on whichever of those get wired to a real vendor first.
  `editor` (video compositing) is the one exception: it needs no vendor
  account at all, so its real ffmpeg-based provider
  (`libs/providers/editor/ffmpeg_provider.py`) already composites
  images/video clips + narration + supplementary audio + burned-in
  captions into a final MP4 — Ken Burns-style motion and distinct
  wipe/slide/zoom/dissolve transitions (today collapsed to a plain fade)
  remain a documented future enhancement, not a blocker.
- Publisher Agent: manual trigger, no auto-scheduling.
- A human approval checkpoint at **every** stage transition (safest possible
  starting posture).

**Exit criteria:** one real video produced end-to-end and manually uploaded to
YouTube through the pipeline, with a human reviewing each stage's output
before it proceeds.

## Phase 2 — Automation & QA (~2–3 weeks)

- QA Agent: automated technical checks (sync, silence, duration, resolution,
  loudness) plus the LLM-based policy review.
- Retry-routing logic in the Orchestrator (`FAILED_QA` → correct upstream
  stage, bounded by `retry_count`).
- Admin dashboard (FastAPI + HTMX): project list, per-stage status, approve/
  reject actions, job history view.
- Error handling hardening: Sentry integration, stuck-job sweep, Telegram/
  Slack alerting on exhausted retries.
- Provider fallback chains wired up for LLM and TTS (`provider_configs`
  priority order actually exercised, not just modeled).

**Exit criteria:** a failed QA check automatically routes back and retries
without manual intervention; an operator can approve/reject entirely from the
dashboard; a simulated provider outage demonstrably fails over.

## Phase 3 — Publishing automation & analytics (~2 weeks)

- Auto-publish path: the manual-approval gate becomes a config toggle,
  disabled once trust in QA is established (can be re-enabled per channel at
  any time).
- Analytics Agent: its independent scheduling (Celery beat sweep of
  `PUBLISHED` projects, decoupled from the Manager) already exists as of
  the video-consolidation refactor — this phase adds the real YouTube
  Analytics/Data API pulls into `performance_metrics` behind it.
- Dashboard additions: pipeline status board, per-video performance charts.

**Exit criteria:** a video can go from idea to published on YouTube with zero
manual steps (when the approval gate is off), and its performance is visible
in the dashboard within a day of publishing.

## Phase 4 — Optimization / feedback loop (~3–4 weeks)

- Feedback loop: Research Agent's scoring step incorporates
  `performance_metrics` (which topics/hook styles/thumbnail styles actually
  performed), not just LLM judgment in a vacuum.
- Live trend scraping (YouTube trending, Google Trends, Reddit, RSS) replacing
  the Phase 1 seed-list approach.
- AI image/video-gen provider integration for visuals beyond stock footage.
- Automated thumbnail/title A/B variant testing.
- Per-provider cost tracking dashboard (`provider_usage_log` surfaced as
  spend-by-provider, spend-by-video).
- Multi-channel support exercised for real, if managing more than one
  niche/channel.

**Exit criteria:** the system measurably prefers idea types and creative
choices that outperformed the channel average in the prior period, without a
human tuning prompts by hand.

## Phase 5 — Hardening & scale (ongoing)

- Load/performance testing of the render pipeline; horizontal scaling of
  worker containers (`docker compose up --scale agent_video=N`), or moving
  that one agent to a beefier/GPU-equipped host if needed — the Rendering
  module is still the CPU/RAM-hungry part even though it now shares a
  container with the other five video pipeline modules.
- Backup/restore drills: nightly `pg_dump` + MinIO sync to off-VPS storage
  (Hetzner Storage Box / Backblaze B2), with a documented, tested restore
  runbook — not just a cron job nobody has verified.
- Security review: secrets rotation, least-privilege API key scopes,
  dependency vulnerability scanning in CI.
- Observability upgrade (Prometheus + Grafana + Flower) once operating
  unattended for long stretches makes richer dashboards worth the added
  operational surface (see [Technology Choices §5.7](./05-technology-choices.md#57-why-not-more-sooner)).

This phase has no fixed exit criteria — it's the steady-state maintenance and
scaling work that continues once the system is live.
