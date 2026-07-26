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
`prompts/README.md`. Every such call is also wrapped in
`libs.llm_usage.track_llm_call(...)`, which records one `LLMUsageLog` row
per API call — provider, model, resolved prompt version, input/output
tokens, elapsed time, estimated cost — against the project it was spent on,
success or failure alike, for future analytics/optimization; see the
`libs/llm_usage/` entry in [Folder Structure](./02-folder-structure.md) and
the `llm_usage_log` table in [Database Design §4.2](./04-database-design.md).

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

Its only responsibility is finding high-potential video ideas — it doesn't
write scripts, produce media, or make publishing decisions. Implemented
(services/agent_research), unlike most other agents at this point, and
runs in one of two modes:

- **Enrich mode** (Manager-dispatched, one project at a time): the Manager
  already created a `Project` and a `VideoIdea` row for a human-submitted
  goal (`ManagerAgent.receive_goal`, §3.1) before this job runs. The agent
  elaborates on that one goal — trend context, keywords, audience, angle,
  competition, score, length, notes — and updates the same row.
- **Discover mode** (channel-level, no project yet): generates several new
  candidate ideas from trend signals/seed topics and inserts each as a new
  `video_ideas` row (`status=proposed`, awaiting approval). Dispatched via
  `agents.research.discover`, not by the Manager — nothing schedules this
  automatically yet (the "daily ideation run per channel" from
  [System Architecture §1.5](./01-system-architecture.md), step 1, is a
  natural follow-up once something calls it).

- **Input:** channel config (niche, persona, banned topics, seed topics —
  read from `Channel.persona_config`), plus a specific goal (enrich mode)
  or a seed-topic/count request (discover mode).
- **Does:**
  - **Analyzes trends** via a pluggable set of trend sources
    (`trend_sources/`): only a configured seed-topic list is real today
    (`seed_list.py`) — YouTube Trending, Google Trends, Reddit, and RSS
    are honest `NotImplementedError` stubs, landing in Phase 4 (live
    scraping). A source being unavailable is non-fatal: the aggregator
    degrades to fewer sources rather than failing the run.
  - **Generates topics and scores them** in one call to Claude
    (`idea_generator.py`, forced tool use, prompts loaded from
    `prompts/research/` via `libs/prompts`) — topic, target audience, why
    people would watch, keywords, suggested angle, competition level,
    score, suggested length, and research notes, all from one structured
    response. There is no deterministic fallback here (unlike the
    Manager's reasoning engine): a missing key, API failure, refusal, or
    malformed response all raise and fail the job honestly — inventing a
    placeholder idea would defeat the entire point of this agent.
  - **Avoids duplicate content** with a lexical similarity check
    (`dedup.py`: title similarity + keyword overlap against this
    channel's existing ideas) — not yet the embedding/pgvector semantic
    search in the original design (§4.2 below); that needs a real
    embedding provider, which isn't configured. A likely duplicate is
    still stored (a human can judge it) with a note and a capped score,
    not silently dropped.
  - **Builds a Knowledge Package** (enrich mode only —
    `knowledge_builder.py`) — deep, source-grounded research on the
    now-finalized topic: verified facts, a timeline, entities, citations,
    keywords, related topics, hooks, and supporting notes (schema:
    `libs/schemas/knowledge.py`). This is the one Claude call in the whole
    pipeline that includes Anthropic's server-side `web_search`/`web_fetch`
    tools and does **not** force `tool_choice` — forcing it would leave no
    room for Claude to actually search before answering, and a "verified
    fact" that's really just an unforced guess isn't verified. A long
    research turn that pauses mid-way (`stop_reason: "pause_turn"`) is
    resumed automatically, bounded to a few continuations. Stored as JSON
    (source of truth) + a Markdown rendering generated from it via
    `libs.storage`, keyed by project id
    (`{project_id}/research/knowledge_package.{json,md}`), with
    `VideoIdea.knowledge_package_json_path`/`.knowledge_package_md_path`
    pointing at them — so the Script Agent can load
    `KnowledgePackage.model_validate_json(...)` directly instead of
    researching the topic itself. Discover mode skips this: running a full
    web-search-backed research pass on every unapproved candidate idea
    would be expensive and mostly wasted. No fallback here either — more so
    than idea generation, since fabricating "verified facts" with fake
    citations would be actively harmful, not just low-value.
- **Output:** `video_ideas` rows — topic (`title`), `target_audience`,
  `rationale` (why people would watch), `keywords`, `suggested_angle`,
  `competition_level`, `score`, `suggested_length_sec`, `research_notes`,
  plus (enrich mode) the Knowledge Package path columns above.
- **Failure mode:** a trend source being unreachable is non-fatal (see
  above); an idea-generation or knowledge-package failure fails the job,
  which the Manager's reasoning engine then retries or escalates like any
  other agent failure.
- **Prompts:** `prompts/research/generate_ideas_system/` +
  `generate_ideas_user/` (idea generation), and
  `prompts/research/build_knowledge_package_system/` +
  `build_knowledge_package_user/` (the Knowledge Package) — each system
  prompt has a Claude-specific override.

## 3.3 Script Writing Agent

Implemented (`services/agent_scriptwriter`). Its only responsibility is
turning one already-researched, approved idea into a complete,
production-ready script — it does not choose the topic or do its own
research; the Research Agent (§3.2) already did that.

- **Input:** always Manager-dispatched against an existing `Project`, with
  an empty payload (`ManagerAgent._advance` dispatches every stage with
  `{}` — see `services/orchestrator/app/manager/manager.py`). The agent
  loads everything it needs itself via
  `libs.context.build_project_context(project_id)` — Channel Profile,
  Project metadata, Research summary, Knowledge Package (when the idea has
  one), resolved prompt version, and Manager settings, gathered into a
  single immutable `ProjectContext` in one call — instead of separately
  querying the channel, project, idea, and knowledge package itself the way
  `services/agent_research/app/worker.py` does. See
  [Folder Structure](./02-folder-structure.md)'s `libs/context/` entry.
- **Does:** one structured Claude call (`script_generator.py`, forced tool
  use, prompts loaded from `prompts/script/` via `libs/prompts`) that
  returns a complete script in one response:
  - a strong opening **hook**, an **introduction** that earns the promise
    the hook made, a body broken into as many **main sections** as the
    topic needs, an **ending** that closes the throughline the hook
    opened, and a single, specific **call to action**;
  - a deliberately chosen **story structure** (e.g. problem → agitation →
    solution, chronological case study, before/after/bridge), explained in
    `structure_notes` rather than left implicit;
  - **retention techniques** — open loops, pattern interrupts, callbacks,
    curiosity gaps — woven through the script itself and documented
    concretely (with placement) in `retention_notes`, rather than treated
    as a separate segment;
  - every beat (hook, introduction, each main section, ending, CTA)
    carries `voiceover_text` (exact narration, written for spoken
    delivery), a `scene_description` (what's on screen), and
    `visual_suggestions` (concrete b-roll/on-screen-text/shot ideas — never
    generic "add engaging visuals").

  When the idea has a Knowledge Package, the agent writes from its
  verified facts, timeline, entities, and hooks directly instead of
  inventing claims of its own — the same "don't fabricate" discipline the
  Research Agent's `knowledge_builder.py` follows. Like `idea_generator.py`,
  there is no deterministic fallback: a missing API key, a failed call, a
  refusal, an empty `main_sections` list, or a malformed response all raise
  `ScriptGenerationError` and fail the job honestly — a fabricated
  placeholder script would defeat the entire point of this agent.
- **Output:** a versioned `scripts` row (`content` — the full narration,
  concatenated in order; `structure_notes`; `retention_notes`;
  `target_duration_sec`; `word_count`; `status=draft`) plus one ordered
  `script_segments` row per beat (`segment_type` — hook / introduction /
  main_section / ending / call_to_action; `text` — the voiceover;
  `scene_notes` — the scene description, prefixed with the section's
  internal heading for main sections; `visual_notes` — the visual
  suggestions; `estimated_duration_sec` — a rough word-count-based
  estimate for the Storyboard module to plan shot lengths with, before
  real voiceover audio exists). Scripts are versioned, never mutated in
  place — a regeneration for the same project gets `version = max(existing) +
  1`, so a QA-triggered rewrite never overwrites what it's replacing.
- **Failure mode:** any Claude-call failure raises `ScriptGenerationError`
  and fails the job, which the Manager's reasoning engine then retries or
  escalates like any other agent failure — no automatic re-prompt inside
  this agent itself.
- **Prompts:** `prompts/script/generate_script_system/` +
  `generate_script_user/`, each with a Claude-specific system-prompt
  override (`v1.claude.yaml`) noting the forced `tool_choice`, same pattern
  as `prompts/research/generate_ideas_system/`.

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
