# 3. Agent Responsibilities

Every agent is a Celery worker: it consumes jobs from exactly one queue, does
one kind of work, writes its output to the database/centralized asset storage
(`libs/storage`), and reports success or failure. No agent calls another
agent directly, and no agent decides the next pipeline stage — that's the
Manager Agent's job (see
[System Architecture §1.4](./01-system-architecture.md#14-orchestration-pattern-centralized-not-choreographed)).
Any agent that calls an LLM loads its prompt from a versioned template file
(`prompts/<agent>/<name>/`) through the Prompt Management System
(`libs/prompts`) rather than embedding the wording in its own module — see
`prompts/README.md`.

---

## 3.1 Manager Agent (coordinator, not a queue-consuming "agent" per se)

- Owns the pipeline state machine and every stage transition — the fixed
  plan is Research → Script → Video → Quality Check → Publishing (five
  stages; see services/orchestrator/app/manager/workflow.py). Agents never
  call each other or self-advance; they report a result and the Manager
  decides what happens next (§1.4).
- Uses Claude as its reasoning engine to decide advance/retry/escalate/abort
  at every stage transition, with a deterministic fallback rule if the
  Claude call itself is unavailable (services/orchestrator/app/manager/reasoning.py).
  Both prompts it sends — the system prompt and the per-decision context —
  are loaded at runtime from `prompts/manager/` via `libs/prompts`, with a
  Claude-specific override for the system prompt
  (`workflow_decision_system/v1.claude.yaml`); a missing/broken template
  file falls back to the same deterministic rule as a Claude API outage,
  never a crash.
- Applies retry policy: how many times a failed stage retries (bounded by
  `Project.retry_count` vs. a configured ceiling) before escalating to
  human review, and which upstream stage a `FAILED_QA` result routes back to.
- Enforces human-in-the-loop approval gates, driven by per-channel config
  (`require_approval_before_publish: true/false`, etc.) rather than code.
- Exposes the internal API (`POST /goals`, plus projects/jobs routers) the
  dashboard and external callers use to submit goals, list projects, view
  job history, and approve/reject content.
- Does **not** run the Analytics Agent's schedule or gate anything on it —
  that runs entirely independently of the Manager (see §3.10). The
  Manager's involvement in a project ends once it reaches `PUBLISHED`.

## 3.2 Research / Ideation Agent

- **Input:** channel config (niche, persona, banned topics, content cadence).
- **Does:** pulls trend signals (YouTube trending API, Google Trends, Reddit,
  configured RSS feeds), clusters and scores candidate topics with an LLM call
  against the channel's persona and past performance data, and checks each
  candidate for semantic duplication against previously produced videos
  (embedding similarity, pgvector).
- **Output:** ranked `video_ideas` rows (title candidates, target keywords,
  rationale, confidence score).
- **Failure mode:** a trend source being unreachable is non-fatal — the agent
  degrades to fewer sources rather than failing the whole run; an LLM scoring
  failure fails the job and is retried with backoff.
- **Prompt:** `prompts/research/idea_scoring/` (draft — not wired into real
  code yet, see §6 Phase 1).

## 3.3 Script Writing Agent

- **Input:** an approved `video_idea`.
- **Does:** generates a full script from a configurable prompt template (hook →
  body → CTA → outro) honoring brand voice/tone config; breaks the script into
  timed segments for the Video Agent's storyboard module (§3.4.1); optionally
  runs a fact-check pass
  (second LLM call, or retrieval against a trusted source set) for
  claim-heavy niches.
- **Output:** a versioned `scripts` row plus `script_segments`.
- **Failure mode:** LLM output failing schema validation (missing sections,
  wildly wrong length) triggers an automatic single re-prompt before failing
  the job — cheap to retry, expensive to send bad input downstream.
- **Prompt:** `prompts/script/generate_script/` (draft — not wired into real
  code yet, see §6 Phase 1).

## 3.4 Video Agent

One agent, one queue (`video`), one `ProjectStage` (`VIDEO_CREATION`) — but
four internal modules run in sequence within a single job
(services/agent_video/app/video_agent.py), covering what used to be four
separate agents/stages. The Manager dispatches and retries this stage as
one unit; it never sees or retries an individual module. That's a real
trade-off, not just a simplification: assembly (§3.4.3) already depends
on both storyboard's (§3.4.1) and voice-over's (§3.4.2) output, so a
failure there means re-running all four from scratch rather than resuming
mid-way — acceptable because storyboard and voice-over are cheap relative
to assembly.

### 3.4.1 Storyboard / Visual Planning module

- **Input:** `script_segments`.
- **Does:** decides, per segment, the visual treatment — stock footage search
  query, AI image-gen prompt, AI video-gen prompt, or a text/motion-graphics
  overlay — and resolves it to a concrete asset: either sourcing from a stock
  provider or generating one (`libs.providers.get_provider("image_gen")` /
  `"video_gen"`).
- **Output:** `storyboard_shots` (shot list) referencing `assets` rows, in
  script order.
- **Failure mode:** if a preferred visual type is unavailable (e.g. no stock
  match), it falls back to the next configured treatment (e.g. stock → AI
  image) rather than failing the segment.
- **Prompt:** `prompts/video/storyboard_shot_planning/` (draft — not wired
  into real code yet, see §6 Phase 1).

### 3.4.2 Voice-over module

- **Input:** `script_segments`.
- **Does:** sends each segment to the configured TTS provider
  (`libs.providers.get_provider("tts")`; voice selection, pacing, a
  pronunciation-correction dictionary for brand/technical terms),
  normalizes loudness across segments, and captures word-level timestamps
  where the provider supports them (needed for caption sync).
- **Output:** per-segment audio `assets` plus duration/timing metadata,
  written via `libs.storage`.
- **Failure mode:** provider rate-limit or outage triggers the fallback TTS
  provider defined in `provider_configs`/`config/providers.yaml`; a segment
  that still fails after fallback marks the project `NEEDS_HUMAN_REVIEW`
  rather than silently skipping the audio.

### 3.4.3 Assembly module

- **Input:** voice-over audio and storyboard shot list from the two modules
  above (passed directly in-process — no extra job/queue round-trip), plus
  channel branding (intro/outro, background music library, caption style).
- **Does:** composites visuals + voice-over + music + burned-in or soft
  captions + transitions into the final render (ffmpeg-based pipeline); this
  is the most CPU/time-intensive module and the primary driver of VPS sizing.
- **Output:** final rendered video `assets` row (resolution, duration,
  checksum) referenced by a `renders` row.
- **Failure mode:** partial-render crashes fail the whole Video Agent job,
  which the Manager retries from the start (renders are not resumable, and
  neither is the module sequence); a render exceeding a configured max
  duration is killed and flagged rather than left running indefinitely.

### 3.4.4 Thumbnail Generation module

- **Input:** approved script/title, channel style guide (colors, fonts, logo
  placement). Has no real dependency on assembly's output — it runs last
  in the sequence purely to keep `VideoAgent.run()` one straight line, not
  because it needs the render.
- **Does:** generates 2–4 thumbnail candidates via the image-gen provider
  (`libs.providers.get_provider("image_gen")`), composites title
  text/emphasis per the style guide, tags each as a variant for later A/B
  testing.
- **Output:** `thumbnails` rows linked to `assets`, one marked `is_selected`.
- **Failure mode:** falls back to a template-only thumbnail (no AI image, text
  over a branded background) if the image-gen provider fails.
- **Prompt:** `prompts/video/thumbnail_prompt/` (draft — not wired into real
  code yet, see §6 Phase 1).

## 3.8 Quality Assurance Agent

- **Input:** the fully rendered video, thumbnail, and script.
- **Does:** runs automated technical checks (audio/video sync drift, silence
  gaps, duration within target range, resolution/aspect ratio, loudness spec,
  corrupt/black-frame detection) and an LLM-based content-policy review against
  YouTube monetization/community guidelines (flagging medical, violence, or
  misinformation risk) to reduce demonetization/strike risk before anything is
  published.
- **Output:** a `qa_reports` row (pass/fail + itemized issues).
- **Failure mode:** on fail, the report's issue category determines which
  upstream stage the Manager routes back to (e.g. sync drift or audio
  issues → `VIDEO_CREATION`, since voice-over/assembly are now internal
  modules of that one stage rather than separately addressable stages;
  policy flag → `SCRIPTING`), bounded by the project's `retry_count`.
- **Prompt:** `prompts/qa/policy_review/` (draft — not wired into real code
  yet, see §6 Phase 2).

## 3.9 Publisher Agent

- **Input:** an approved, QA-passed project.
- **Does:** manages YouTube OAuth2 token refresh, performs the resumable
  upload via the YouTube Data API v3, sets title/description/tags/category/
  playlist/captions file, uploads the selected thumbnail, and schedules or
  immediately publishes per config. Tracks the Data API's daily quota and
  paces uploads to stay under it.
- **Output:** a `publications` row with the returned `youtube_video_id`.
- **Failure mode:** every upload attempt is keyed by the project's job id so a
  retried job upserts rather than re-uploads — a duplicate publish is treated
  as a correctness bug, not an acceptable retry side effect.

## 3.10 Analytics / Performance Tracking Agent

Not a step in the Manager's workflow plan, and never dispatched by it
(§3.1, §1.3, §1.5 step 9) — this is the one agent in the system that
schedules itself:

- **Trigger:** its own Celery beat process (`agent_analytics_beat`, same
  image as the `agent_analytics` worker, running `celery beat` instead)
  fires a sweep task on a recurring schedule
  (`ANALYTICS_SWEEP_INTERVAL_HOURS`), independent of any per-video
  pipeline event.
- **Input:** every `Publication` row with `publish_status = PUBLISHED`,
  found by the sweep, one dispatched job per publication.
- **Does:** pulls views, watch time, average view duration, CTR, likes,
  comments, and subscriber delta from the YouTube Analytics + Data APIs
  (`libs.providers.get_provider("youtube")`); computes performance
  relative to the channel's rolling average.
- **Output:** time-series `performance_metrics` rows.
- **Isolation from the pipeline:** `AnalyticsAgent.reports_to_manager =
  False` (libs/agents/base.py) — finishing an analytics job never calls
  back into the Manager's advance/retry/escalate decision loop. A failed
  or slow metrics pull cannot affect `Project.current_stage`, `.status`,
  or `.retry_count`; it's just a failed `jobs` row, same audit trail as
  any other agent, with no pipeline-side consequence.
- **Feedback loop:** aggregated performance-by-topic/thumbnail-style/hook-style
  is the primary signal the Research Agent's scoring step uses to prefer
  what has actually worked — this closed loop is what makes the system
  self-improving rather than a one-shot pipeline (see
  [Roadmap, Phase 4](./06-roadmap.md)).
