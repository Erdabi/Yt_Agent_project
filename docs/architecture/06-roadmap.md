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
- Provider abstraction interfaces in `libs/providers/*` defined but backed by
  stub/mock implementations only — this lets every later agent be built
  against a stable interface before real API keys/costs are involved.
- Alembic baseline migration for the core schema
  ([Database Design §4.2](./04-database-design.md#core-tables)).
- CI pipeline skeleton (lint, type-check, test, build images).

**Exit criteria:** `docker compose up` brings up a healthy empty stack;
`alembic upgrade head` runs clean; CI is green on an empty test suite.

## Phase 1 — MVP single-path pipeline (~3 weeks)

- Orchestrator with a simple linear state machine (no retry-routing
  sophistication yet).
- Research Agent: manual/seed topic list + one LLM call to expand into
  scored ideas (live trend scraping deferred to Phase 4).
- Script Agent with one real LLM provider.
- Voice-over Agent with one real TTS provider.
- Video Assembly Agent: static images/stock footage + Ken Burns-style motion +
  burned-in captions via ffmpeg (no AI-generated visuals yet).
- Thumbnail Agent: template + text overlay only (no AI image generation yet).
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
- Analytics Agent: scheduled YouTube Analytics/Data API pulls into
  `performance_metrics`.
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
  worker containers (`docker compose up --scale agent_video_assembly=N`), or
  moving that one agent to a beefier/GPU-equipped host if needed.
- Backup/restore drills: nightly `pg_dump` + MinIO sync to off-VPS storage
  (Hetzner Storage Box / Backblaze B2), with a documented, tested restore
  runbook — not just a cron job nobody has verified.
- Security review: secrets rotation, least-privilege API key scopes,
  dependency vulnerability scanning in CI.
- Observability upgrade (Prometheus + Grafana + Flower) once operating
  unattended for long stretches makes richer dashboards worth the added
  operational surface (see [Technology Choices §5.6](./05-technology-choices.md#56-why-not-more-sooner)).

This phase has no fixed exit criteria — it's the steady-state maintenance and
scaling work that continues once the system is live.
