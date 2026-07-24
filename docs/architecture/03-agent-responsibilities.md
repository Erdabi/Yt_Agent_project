# 3. Agent Responsibilities

Every agent is a Celery worker: it consumes jobs from exactly one queue, does
one kind of work, writes its output to the database/object storage, and
reports success or failure. No agent calls another agent directly, and no
agent decides the next pipeline stage — that's the Orchestrator's job (see
[System Architecture §1.4](./01-system-architecture.md#14-orchestration-pattern-centralized-not-choreographed)).

---

## 3.1 Orchestrator (coordinator, not an "agent" per se)

- Owns the pipeline state machine and every stage transition.
- Runs the scheduler (Celery beat) for recurring triggers: daily ideation runs
  per channel, periodic analytics pulls, a sweep for jobs stuck past their
  timeout.
- Applies retry policy: how many times a failed stage retries, and which
  upstream stage a `FAILED_QA` result routes back to.
- Enforces human-in-the-loop approval gates, driven by per-channel config
  (`require_approval_before_publish: true/false`, etc.) rather than code.
- Exposes the internal API the dashboard uses to list projects, view job
  history, and approve/reject content.

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

## 3.3 Script Writing Agent

- **Input:** an approved `video_idea`.
- **Does:** generates a full script from a configurable prompt template (hook →
  body → CTA → outro) honoring brand voice/tone config; breaks the script into
  timed segments for the Storyboard agent; optionally runs a fact-check pass
  (second LLM call, or retrieval against a trusted source set) for
  claim-heavy niches.
- **Output:** a versioned `scripts` row plus `script_segments`.
- **Failure mode:** LLM output failing schema validation (missing sections,
  wildly wrong length) triggers an automatic single re-prompt before failing
  the job — cheap to retry, expensive to send bad input downstream.

## 3.4 Storyboard / Visual Planning Agent

- **Input:** `script_segments`.
- **Does:** decides, per segment, the visual treatment — stock footage search
  query, AI image-gen prompt, AI video-gen prompt, or a text/motion-graphics
  overlay — and resolves it to a concrete asset: either sourcing from a stock
  provider or enqueuing a generation request.
- **Output:** `storyboard_shots` (shot list) referencing `assets` rows, in
  script order.
- **Failure mode:** if a preferred visual type is unavailable (e.g. no stock
  match), it falls back to the next configured treatment (e.g. stock → AI
  image) rather than failing the segment.

## 3.5 Voice-over Agent

- **Input:** `script_segments`.
- **Does:** sends each segment to the configured TTS provider (voice selection,
  pacing, a pronunciation-correction dictionary for brand/technical terms),
  normalizes loudness across segments, and captures word-level timestamps
  where the provider supports them (needed for caption sync).
- **Output:** per-segment audio `assets` plus duration/timing metadata.
- **Failure mode:** provider rate-limit or outage triggers the fallback TTS
  provider defined in `provider_configs`; a segment that still fails after
  fallback marks the project `NEEDS_HUMAN_REVIEW` rather than silently
  skipping the audio.

## 3.6 Video Assembly Agent

- **Input:** voice-over audio, storyboard shot list, channel branding
  (intro/outro, background music library, caption style).
- **Does:** composites visuals + voice-over + music + burned-in or soft
  captions + transitions into the final render (ffmpeg-based pipeline); this
  is the most CPU/time-intensive stage and the primary driver of VPS sizing.
- **Output:** final rendered video `assets` row (resolution, duration,
  checksum) referenced by a `renders` row.
- **Failure mode:** partial-render crashes are retried from scratch (renders
  are not resumable); a render exceeding a configured max duration is killed
  and flagged rather than left running indefinitely.

## 3.7 Thumbnail Generation Agent

- **Input:** approved script/title, channel style guide (colors, fonts, logo
  placement).
- **Does:** generates 2–4 thumbnail candidates via the image-gen provider,
  composites title text/emphasis per the style guide, tags each as a variant
  for later A/B testing.
- **Output:** `thumbnails` rows linked to `assets`, one marked `is_selected`.
- **Failure mode:** falls back to a template-only thumbnail (no AI image, text
  over a branded background) if the image-gen provider fails, so the pipeline
  is never blocked purely on thumbnail generation.

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
  upstream stage the Orchestrator routes back to (e.g. sync drift →
  `VOICEOVER`/`VIDEO_ASSEMBLY`, policy flag → `SCRIPTING`), bounded by the
  project's `retry_count`.

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

- **Input:** all `PUBLISHED` projects (runs on its own recurring schedule, not
  the per-video pipeline).
- **Does:** pulls views, watch time, average view duration, CTR, likes,
  comments, and subscriber delta from the YouTube Analytics + Data APIs;
  computes performance relative to the channel's rolling average.
- **Output:** time-series `performance_metrics` rows.
- **Feedback loop:** aggregated performance-by-topic/thumbnail-style/hook-style
  is the primary signal the Research Agent's scoring step uses to prefer
  what has actually worked — this closed loop is what makes the system
  self-improving rather than a one-shot pipeline (see
  [Roadmap, Phase 4](./06-roadmap.md)).
