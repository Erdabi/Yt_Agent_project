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
    generic "add engaging visuals"), plus structured **production
    metadata** the Video Agent can act on with no further parsing:
    `camera_framing` (free text — "wide shot", "close-up", ...),
    `asset_requirements` (see below), `transition_type`
    (cut/fade/dissolve/wipe/zoom/slide/match_cut), `pacing`
    (fast/medium/slow — editing rhythm, independent of narration speed),
    `narration_emotion` (free text — "curious", "urgent", ...),
    `emphasis_words`, and `estimated_speech_wpm` (bounded 80-220 in the
    tool schema, and clamped again defensively before the worker computes
    `estimated_duration_sec` from it — never trust an LLM-supplied number
    for a downstream calculation without a floor/ceiling, same posture as
    the Manager enforcing its own retry ceiling). See `script_schema.py`'s
    `SegmentProductionMetadata`.
  - **`asset_requirements`** is a structured, *provider-independent* list
    of every concrete production asset a beat needs — a beat can need
    more than one at once (e.g. a stock video clip AND a sound effect AND
    a text overlay). Each entry is an `AssetRequirement`: an `asset_type`
    (one of eleven values — `ai_video`, `ai_image`, `stock_footage`,
    `animation`, `diagram`, `map`, `portrait`, `text_overlay`,
    `subtitle_emphasis`, `sound_effect`, `background_music_cue`) plus a
    content `description` ("close-up of a chisel bevel at a 25-degree
    angle") — never which provider or tool should supply it; deciding how
    to satisfy a requirement is the Video Agent's job (via
    `libs.providers`), once its modules land, not this agent's. **Every
    beat must declare at least one visual-category requirement** (any
    `asset_type` other than `sound_effect`/`background_music_cue`)
    covering what its `scene_description`/`visual_suggestions` describes
    — enforced in code (`script_schema.py`'s `_validate_asset_coverage`),
    not just prompted for, since every beat structurally always describes
    a visual (both fields are required, non-empty) and a JSON schema
    alone can't express "and at least one list entry must be one of these
    enum values." A beat that fails this check makes `script_generator.py`
    raise `ScriptGenerationError` for an initial draft, or makes
    `script_reviewer.py` fall back to the prior, already-valid draft for
    a revision — the same graceful-degradation path any other malformed
    review response takes.

  When the idea has a Knowledge Package, the agent writes from its
  verified facts, timeline, entities, and hooks directly instead of
  inventing claims of its own — the same "don't fabricate" discipline the
  Research Agent's `knowledge_builder.py` follows. Like `idea_generator.py`,
  there is no deterministic fallback for this draft call: a missing API
  key, a failed call, a refusal, an empty `main_sections` list, or a
  malformed response all raise `ScriptGenerationError` and fail the job
  honestly — a fabricated placeholder script would defeat the entire
  point of this agent.
- **Self-reviews before persisting:** every draft passes through a second,
  lightweight Claude call (`script_reviewer.py`) before anything is
  written to the database. Given the full draft and the Knowledge
  Package, it checks factual consistency (any claim the package doesn't
  support gets softened or removed), viewer retention (the hook actually
  earns attention, open loops get resolved, the retention techniques
  `retention_notes` claims are actually present in the segments), and
  repetition (an idea/phrase/visual restated across segments gets said
  once, well, instead of twice) — then returns the same full script
  structure back, corrected where needed and left alone where it already
  worked, plus `review_notes` explaining what it checked and changed.
  Unlike generation, review failure does **not** fail the job: the draft
  it's reviewing is already valid and complete, so any failure (no key,
  API error, refusal, malformed response) logs a warning and falls back
  to the original, unreviewed draft — with `review_notes` recording that
  the pass was skipped and why, so that's never silently indistinguishable
  from "reviewed, nothing to fix." A review step that could block an
  already-good script from being persisted would defeat its own "keep it
  lightweight" premise.
- **Output:** a versioned `scripts` row (`content` — the full narration,
  concatenated in order; `structure_notes`; `retention_notes`;
  `review_notes`; `target_duration_sec`; `word_count`; `status=draft`)
  plus one ordered `script_segments` row per beat (`segment_type` — hook /
  introduction / main_section / ending / call_to_action; `text` — the
  voiceover; `scene_notes` — the scene description, prefixed with the
  section's internal heading for main sections; `visual_notes` — the
  visual suggestions; `production_metadata` — the structured JSONB bundle
  above; `estimated_duration_sec` — computed from word count and that
  beat's own `estimated_speech_wpm`, for the Storyboard module to plan
  shot lengths with before real voiceover audio exists). Scripts are
  versioned, never mutated in place — a regeneration for the same project
  gets `version = max(existing) + 1`, so a QA-triggered rewrite never
  overwrites what it's replacing.
- **Failure mode:** any generation-call failure raises
  `ScriptGenerationError` and fails the job, which the Manager's reasoning
  engine then retries or escalates like any other agent failure — no
  automatic re-prompt inside this agent itself. A review-call failure, as
  above, degrades to the unreviewed draft rather than failing the job.
- **Prompts:** `prompts/script/generate_script_system/` +
  `generate_script_user/` (drafting), and
  `prompts/script/review_script_system/` + `review_script_user/`
  (self-review) — each system prompt has a Claude-specific override
  (`v1.claude.yaml`) noting the forced `tool_choice`, same pattern as
  `prompts/research/generate_ideas_system/`. Both prompt pairs are
  versioned together under one `SCRIPT_PROMPT_VERSION` setting.

## 3.4 Video Agent

One agent, one queue (`video`), one `ProjectStage` (`VIDEO_CREATION`) —
but internally, video production is a pipeline of six modules, run in
sequence within a single job (services/agent_video/app/video_agent.py),
plus a seventh (Thumbnail Generation) that runs alongside it:

    Asset Planning -> Asset Generation -> Voice Generation
        -> Subtitle Generation -> Timeline Building -> Rendering

The Manager dispatches and retries this stage as one unit; it never sees
or retries an individual module. Each module has one clearly typed input
and one clearly typed output
(services/agent_video/app/pipeline_schema.py) — no module reaches into
another's internals, and only two modules (Asset Generation, Voice
Generation) ever call `libs.providers.get_provider(...)` at all. That is
what makes a provider swappable independently: changing which class
backs `tts` in `config/providers.yaml` only touches Voice Generation's
own call site, because Timeline Building and Rendering never see a
provider, only the `VoiceSegment`/`ResolvedAsset` values Voice
Generation/Asset Generation already resolved.

The real dependency chain: Asset Generation needs Asset Planning's plan;
Subtitle Generation needs Voice Generation's durations/timing; Timeline
Building needs all three of Asset Generation, Voice Generation, and
Subtitle Generation; Rendering needs Timeline Building. Asset
Planning/Asset Generation have no real dependency on Voice
Generation/Subtitle Generation (or vice versa) — they could run in
parallel — but `VideoAgent.run()` still sequences everything in one
straight line, the same trade-off the previous four-module design already
made explicitly for Thumbnail Generation: one job, one linear sequence,
rather than concurrency inside a single Celery task for a modest latency
win. A failure partway through (e.g. Rendering breaks) means the *whole*
video job is retried by the Manager, including every already-succeeded
module's work — acceptable because most of these modules are fast/cheap
relative to Rendering, and a partial-video retry was never really "resume
where it broke" anyway, since Timeline Building's output depends on all
of them regardless.

Module logic itself (deciding what's needed, computing timing, building a
render plan) is real and tested end to end. Only the true external
dependencies each module's own leaf call needs remain honest
`NotImplementedError`s — a real TTS/image-gen/video-gen/stock-media/
audio-library vendor (all currently `stub` in `config/providers.yaml`)
and a real compositor (ffmpeg is not installed in the Video Agent's image
yet). Swapping in real providers exercises the same module code already
verified against fake ones — nothing about the pipeline itself needs to
change.

**Asset Cache.** Sitting directly in front of both provider-calling
modules' leaf calls is `services/agent_video/app/asset_cache.py`'s
`AssetCache` — before Asset Generation or Voice Generation calls a
provider, it checks this cache for an asset already produced from an
identical semantic request, and reuses it instead of regenerating it.
"Identical" is a SHA-256 hash of a canonical JSON payload
(`AssetCacheKey`) covering everything that determines the output:
`capability`, `provider_name`, `asset_type`, `prompt`, and whichever of
`resolution`/`duration_sec`/`style`/`language`/`settings` apply — so a
provider swap, or any change to the request itself, naturally produces a
different hash rather than wrongly reusing another vendor's or another
request's output. The cache mechanism itself stays generic (no
per-provider branching lives in `asset_cache.py`); `capability` and
`provider_name` are just data fields on the key.

The cache is deliberately global, not scoped to one project — indexed by
its own `asset_cache_entries` table (`libs/models/asset_cache.py`, no
foreign key to `projects` or `assets`), with cached bytes stored under
their own non-project storage namespace. Two different projects
requesting the same prompt/provider/settings combination share the same
stored bytes; each still gets its own `assets`/`storyboard_shots`/
`voiceovers` rows on every request, cache hit or miss — only the
underlying `storage_path` is shared. A cache hit for TTS also carries
`duration_sec`/`word_timings` in the entry's `metadata` column, so Voice
Generation can rebuild a complete result without resynthesizing. Writing
a new entry tolerates losing a race to another worker caching the same
key concurrently (the `cache_key` unique-constraint violation is caught
and logged, not raised) — the caller's own freshly produced bytes remain
valid regardless of whether its own index row won that race.

### 3.4.1 Asset Planning module

- **Input:** `script_segments`, specifically each segment's
  `production_metadata.asset_requirements` (see §3.3's Script Agent
  section, and `libs/schemas/script_production.py`).
- **Does:** routes each requirement's provider-independent `asset_type`
  (11 values — `ai_video`, `ai_image`, `stock_footage`, `animation`,
  `diagram`, `map`, `portrait`, `text_overlay`, `subtitle_emphasis`,
  `sound_effect`, `background_music_cue`) to the `libs.providers`
  capability that satisfies it (`ASSET_TYPE_ROUTING` in
  `pipeline_schema.py`) and to the legacy, visual-only `ShotType`
  (`stock`/`ai_image`/`ai_video`/`text_overlay`) a resolved shot
  collapses to for `storyboard_shots.shot_type`. `text_overlay` routes to
  no provider at all (composited directly at render time);
  `subtitle_emphasis` isn't planned as an asset at all — it's a
  caption-styling instruction Subtitle Generation reads directly. Pure
  computation: no provider calls, no database writes, so this module is
  fully real regardless of which providers happen to be configured for
  anything downstream.
- **Output:** a list of planned assets (one per resolvable requirement),
  each carrying its capability/shot-type routing — not yet resolved to
  any actual file.
- **Failure mode:** none of its own — a malformed/missing routing can
  only happen if the Script Agent's schema itself changed incompatibly,
  which is caught by that agent's own validation (§3.3), not here.

### 3.4.2 Asset Generation module

- **Input:** Asset Planning's output.
- **Does:** for every provider-generated requirement, first checks the
  Asset Cache (see above) for an identical prior request; on a hit,
  reuses its `storage_path` and skips the provider call entirely. On a
  miss, calls the configured provider for that capability
  (`libs.providers.get_provider("image_gen"/"video_gen"/"stock_media"/
  "audio_library")`) and caches the result. Either way, persists an
  `assets` row plus (for visual requirements) a `storyboard_shots` row.
  This is the *only* module that calls those four capabilities —
  Asset Planning already decided *which* one each requirement needs, so
  swapping the concrete class behind any of them is a
  `config/providers.yaml` change that touches nothing else in this
  pipeline.
- **Output:** a list of resolved assets — an asset id, a storage path,
  which provider produced it — one per planned asset (`None` path for a
  `text_overlay` entry, which produced no file).
- **Failure mode:** a provider failure propagates and fails the job
  honestly, same as every other agent in this pipeline — the Manager's
  reasoning engine decides whether to retry or escalate.

### 3.4.3 Voice Generation module

- **Input:** `script_segments` — runs independently of Asset
  Planning/Asset Generation; narration has no dependency on which visual
  assets a segment resolves to.
- **Does:** first checks the Asset Cache for an identical prior request
  (same text/provider); on a hit, reconstructs duration/word timings from
  the cache entry's metadata instead of resynthesizing. On a miss, sends
  the segment's text to the configured TTS provider
  (`libs.providers.get_provider("tts")`) and caches the audio plus its
  duration/timing. Either way, persists the audio as an `assets` row plus
  a `voiceovers` row. This is the *only* module that calls `tts` —
  swapping ElevenLabs for Azure Speech touches nothing else in this
  pipeline, since every other module only ever sees the duration/timing
  this module resolved, never the vendor.
- **Output:** a list of voice segments — an asset id, a storage path, a
  duration, and (when the provider supports it) per-word timing. Falls
  back to the Script Agent's own `estimated_speech_wpm` pacing estimate
  when a provider returns no duration/timing of its own, rather than
  requiring every provider to support it.
- **Failure mode:** a provider failure propagates and fails the job.

### 3.4.4 Subtitle Generation module

- **Input:** `script_segments` plus Voice Generation's output — a
  genuine dependency, since caption timing needs the durations/timing
  Voice Generation already produced.
- **Does:** splits each segment's narration into caption-sized cues
  (a handful of words at a time), timed either from the TTS provider's
  real per-word timestamps when available, or an even split of the
  segment's known duration across its words otherwise — this module
  never requires provider-level timestamps to function correctly, only
  benefits from more precise ones when they exist. Marks a cue for
  emphasis styling when one of its words matches the segment's
  `emphasis_words`, or the whole segment carries a `subtitle_emphasis`
  asset requirement. Makes no provider calls of its own.
- **Output:** a list of caption cues, timed *segment-relative* (0 = the
  moment that segment's audio starts) — Timeline Building is the only
  module that shifts these to absolute project time.
- **Failure mode:** none of its own; a segment Voice Generation didn't
  cover simply gets no cues rather than raising.

### 3.4.5 Timeline Building module

- **Input:** `script_segments`, Asset Generation's resolved assets, Voice
  Generation's voice segments, Subtitle Generation's cues.
- **Does:** the single place that computes cumulative, absolute project
  timing. Every upstream module works in segment-relative terms
  precisely so this arithmetic exists in exactly one place instead of
  being re-derived (and risking drifting out of sync) in several. A
  segment's narration audio is the authoritative driver of its length —
  visuals get stretched/looped/trimmed to fill whatever duration Voice
  Generation produced, not the other way around. Pure data assembly, no
  provider calls, no external dependency of any kind — fully real and
  testable regardless of which providers are configured for anything
  upstream.
- **Output:** a `Timeline` — an ordered list of entries (one per
  segment), each with its absolute start/end time, visual assets,
  supplementary audio (sound effects/music), shifted subtitle cues, and
  the segment's own transition/pacing/camera-framing metadata.
- **Failure mode:** raises if a segment has no corresponding voice
  segment — the whole timing model depends on narration duration for
  every segment, so this is treated as a genuine data-integrity failure,
  not something to silently skip past.

### 3.4.6 Rendering module

- **Input:** the `Timeline`, plus channel branding (intro/outro,
  caption style) read from `Channel.persona_config`.
- **Does:** builds a render plan from the Timeline — resolving each
  entry's visuals, layering voice-over/supplementary audio/captions,
  applying transitions — then invokes a compositor on it. Never imports
  `libs.providers` at all: every asset it needs was already resolved by
  Asset Generation/Voice Generation, so it has nothing to swap and
  nothing to know about which vendor produced any of it. Building the
  plan is real, tested logic; actually invoking ffmpeg is not — ffmpeg
  is deliberately not installed in this image yet (see
  services/agent_video/requirements.txt), so that one leaf call stays an
  honest `NotImplementedError` until it is, the primary CPU/time-
  intensive step and driver of VPS sizing once it lands.
- **Output:** the final rendered video `assets` row (resolution,
  duration) referenced by a `renders` row.
- **Failure mode:** a compositor crash (or, today, its absence) fails
  the whole Video Agent job, which the Manager retries from the start —
  renders are not resumable, and neither is the module sequence; a
  render exceeding a configured max duration should be killed and
  flagged rather than left running indefinitely, once real rendering
  exists.

### Thumbnail Generation module

Runs last inside `VideoAgent.run()`, alongside — not part of — the
six-module pipeline above: it has no real ordering dependency on any of
it, since it only needs the approved script/title and the channel's
style guide, not anything Rendering produces. Runs after it anyway to
keep `VideoAgent.run()` one simple sequence.

- **Input:** approved script/title, channel style guide (colors, fonts,
  logo placement).
- **Does:** generates 2–4 thumbnail candidates via the image-gen
  provider (`libs.providers.get_provider("image_gen")`), composites
  title text/emphasis per the style guide, tags each as a variant for
  later A/B testing.
- **Output:** `thumbnails` rows linked to `assets`, one marked
  `is_selected`.
- **Failure mode:** falls back to a template-only thumbnail (no AI
  image, text over a branded background) if the image-gen provider
  fails.
- **Prompt:** `prompts/video/thumbnail_prompt/` (draft — not wired into
  real code yet, see §6 Phase 1).

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
