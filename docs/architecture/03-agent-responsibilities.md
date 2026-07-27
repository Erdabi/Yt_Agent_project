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
- Exposes the internal API (`POST /goals`, `POST`/`GET /channels`, plus
  projects/jobs routers) the dashboard and external callers use to create
  channels, submit goals, list projects, view job history, and
  approve/reject content. `POST /channels` is the one channel-scoped
  write in this API that doesn't go through the Manager at all — a
  channel is a prerequisite for `receive_goal`, not a pipeline stage — so
  the route (`services/orchestrator/app/api/channels.py`) writes directly
  via `libs.core.db.sync_session_scope`.
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
plus the Thumbnail Agent — a genuine, independent agent in its own right
(see below), invoked in-process alongside it:

    Asset Planning -> Asset Generation -> Voice Generation
        -> Subtitle Generation -> Timeline Building -> Rendering

The Manager dispatches and retries this stage as one unit; it never sees
or retries an individual module (or the in-process Thumbnail Agent call).
Each module has one clearly typed input and one clearly typed output
(services/agent_video/app/pipeline_schema.py) — no module reaches into
another's internals, and each module calls at most the one
`libs.providers` capability it genuinely needs: Asset Generation calls
`image_gen`/`video_gen`/`stock_media`/`audio_library`, Voice Generation
calls `tts`, Rendering calls `editor` (its compositor), and the Thumbnail
Agent (which runs alongside the six, not part of the chain) calls
`image_gen` independently of Asset Generation's own use of it. That is
what makes a provider swappable independently: changing which class
backs `tts` in `config/providers.yaml` only touches Voice Generation's
own call site, because Timeline Building never sees a provider at all,
only the `VoiceSegment`/`ResolvedAsset` values Voice Generation/Asset
Generation already resolved.

The real dependency chain: Asset Generation needs Asset Planning's plan;
Subtitle Generation needs Voice Generation's durations/timing; Timeline
Building needs all three of Asset Generation, Voice Generation, and
Subtitle Generation; Rendering needs Timeline Building. Asset
Planning/Asset Generation have no real dependency on Voice
Generation/Subtitle Generation (or vice versa) — they could run in
parallel — but `VideoAgent.run()` still sequences everything in one
straight line, the same trade-off the previous four-module design already
made explicitly for thumbnail generation: one job, one linear sequence,
rather than concurrency inside a single Celery task for a modest latency
win. A failure partway through (e.g. Rendering breaks) means the *whole*
video job is retried by the Manager, including every already-succeeded
module's work — acceptable because most of these modules are fast/cheap
relative to Rendering, and a partial-video retry was never really "resume
where it broke" anyway, since Timeline Building's output depends on all
of them regardless; the Asset Cache (below) means a retry's Asset
Generation/Voice Generation/Thumbnail Agent work is often close to
free the second time anyway.

Module logic itself (deciding what's needed, computing timing, building a
render plan) is real and tested end to end, including a real compositor
(`libs/providers/editor/ffmpeg_provider.py`) and the real Thumbnail Agent
described below — the whole pipeline has been verified to produce an
actual playable MP4 and thumbnail image end to end (§3.4.6, this
section's Thumbnail Agent). `video_gen` and `tts` also each have a real,
verified adapter alongside their stub — Runway ML
(`libs/providers/video_gen/runway_provider.py`) and ElevenLabs
(`libs/providers/tts/elevenlabs_provider.py`) — both proven end to end
with a mocked HTTP layer standing in for the vendor's servers:
authentication, request creation, ElevenLabs' single synchronous call
(no polling needed, unlike Runway's async task) or Runway's
create/poll/download flow, and vendor-specific error/retry handling all
live inside that one file each, with both capabilities staying `stub`
until a real `RUNWAY_API_KEY`/`ELEVENLABS_API_KEY` is configured. What
remains stub is `image_gen`/`stock_media`/`audio_library`'s real
*vendor* integrations (currently all `stub` in
`config/providers.yaml`), plus `video_gen`'s InVideo AI/Google Veo and
`tts`'s Azure Speech alternatives — all honest stubs rather than
fabricated integrations, since none has a verifiable public API
contract this codebase could implement against (see each module's own
docstring). Swapping any of these in exercises the same module code
already verified against fake providers; nothing about the pipeline
itself needs to change.

**Asset Cache.** Sitting directly in front of every provider-calling
module/agent's leaf calls is `services/agent_video/app/asset_cache.py`'s
`AssetCache` — before Asset Generation, Voice Generation, or the
Thumbnail Agent calls a provider, it checks this cache for an asset
already produced from an identical semantic request, and reuses it
instead of regenerating it.
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
  (same text, plus whatever settings the provider itself reports as
  output-affecting via `TTSProvider.cache_key_settings()` — e.g.
  ElevenLabs' configured voice/model — so a config change to the default
  voice never wrongly reuses audio generated under a different one); on
  a hit, reconstructs duration/word timings/model/voice id/language from
  the cache entry's metadata instead of resynthesizing. On a miss, sends
  the segment's text to the configured TTS provider
  (`libs.providers.get_provider("tts")`) and caches the audio plus that
  same metadata bundle. Either way, persists the full bundle — provider,
  model, voice id, language, duration, word timings — onto `voiceovers`
  and mirrored into the `assets` row's `metadata` (§4.2), not just
  transiently for cache reuse. This is the *only* module that calls
  `tts` — swapping ElevenLabs for Azure Speech touches nothing else in
  this pipeline, since every other module only ever sees the
  duration/timing this module resolved, never the vendor.
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

- **Input:** the `Timeline`, plus channel branding (intro/outro, music
  bed, caption style) read from `Channel.persona_config`, and a
  `profile_name` (defaults to `"long_form_1080p"`, see **Render
  profiles** below).
- **Does:** builds a render plan from the Timeline — resolving each
  entry's visuals, layering voice-over/supplementary audio/captions,
  applying transitions — then hands that plan to
  `libs.providers.get_provider("editor")`, the *only* capability this
  module calls (see `libs/providers/editor/base.py`'s `EditSpec`). Every
  asset the plan references was already resolved by an earlier module
  (Asset Generation/Voice Generation); Rendering reads each one's bytes
  back via `libs.storage` right before handing them to the provider
  (plan-building itself stays storage-free, like every other module's
  own plan/output types) via `_read_required_asset` — a missing or
  unreadable required asset raises `EditorInputError` naming exactly
  which segment/asset failed, rather than a bare `FileNotFoundError`
  surfacing from deep inside the pipeline. An optional branding asset
  (intro/outro/music bed) that's configured but no longer in storage is
  instead logged and skipped — a missing accessory shouldn't abort an
  otherwise-successful video. Rendering writes the provider's returned
  video bytes back through the same storage abstraction. Swapping the
  compositor is a `config/providers.yaml` change, same as any other
  capability — Rendering itself knows nothing about ffmpeg specifically.
- **Progress reporting:** passes an `on_progress` callback
  (`libs/providers/editor/base.py`'s `ProgressCallback`/`RenderProgress`)
  into the provider call. The provider invokes it once per pipeline
  stage (`normalize_segments` per segment, `assemble`,
  `mix_background_music`, `finalize`, `probe_output`) with a computed
  completion percentage. Rendering's own callback
  (`_log_progress`) just logs each event via structlog — no new DB
  column or polling mechanism was added, since nothing today reads
  progress outside logs; a future UI wanting live progress would swap
  that one callback for something that persists it.
- **Compositor:** unlike every other capability in this pipeline, a
  compositor needs no paid vendor account — ffmpeg is a free local
  binary — so `editor`'s "active" provider
  (`libs/providers/editor/ffmpeg_provider.py`) is a real, working
  implementation from day one, not a stub. It normalizes every visual
  clip (image or video) to the resolved render profile's
  resolution/frame rate and the exact duration its segment needs, mixes
  narration with any supplementary audio per segment, applies a plain
  fade for any non-"cut" transition between segments (distinct wipe/
  slide/zoom/dissolve filtergraphs per `TransitionType` are a documented
  future enhancement, not implemented), concatenates every segment plus
  an optional intro/outro, mixes in an optional whole-video
  background-music bed (looped/trimmed to the total duration, volume-
  reduced so it sits under narration), normalizes the final audio to the
  profile's EBU R128 loudness target (single-pass `loudnorm` — real and
  working, though less precise than a two-pass analyze-then-normalize
  approach — a deliberate simplicity/accuracy tradeoff), and burns in
  every subtitle/overlay cue (emphasized cues in a highlight color) via
  `textfile=` (not an inline `text='...'` literal — sidesteps filter-
  string escaping bugs with quotes/apostrophes in narration text) in one
  final pass over the assembled video.
- **Render profiles:** resolution, fps, loudness target, crossfade
  duration, and subtitle font size travel together as one named,
  swappable `RenderProfile` (`libs/providers/editor/profiles.py`),
  loaded from `config/render_profiles.yaml` via a registry-style
  `get_render_profile(name)` loader mirroring
  `libs/providers/registry.py`'s config-loading pattern. `EditSpec`
  carries a `profile_name` rather than a bare resolution string, so a
  future output format (Shorts, a different aspect ratio) is a new named
  profile entry in that YAML file — never a code change to the
  compositor. Today's two profiles: `long_form_1080p` (1920x1080) and
  `shorts_1080x1920` (1080x1920, portrait).
- **Error handling:** every failure path in the ffmpeg provider raises
  one of three typed exceptions (`libs/providers/editor/base.py`) rather
  than a bare exception — `EditorInputError` (bad/missing/corrupt input,
  empty timeline, unknown profile name — retrying won't help),
  `EditorTimeoutError` (a compositor subprocess exceeded its configured
  time budget), or `EditorRenderError` (the compositor itself failed for
  any other reason, carrying ffmpeg's own stderr for diagnosis). Every
  `subprocess.run` call (ffmpeg and ffprobe alike) passes a per-call
  timeout (`ffmpeg_timeout_sec`, default 300s) rather than one budget
  for the whole render, since individual stages vary hugely in expected
  duration. Every input (visual clip, branding asset, audio track,
  background-music bed) is put through a fast pre-flight decode check
  (`ffmpeg -i <path> -frames:v 1 -f null -`) before the real normalize
  command runs — discovered empirically that looping a corrupt still
  image (`-loop 1`) can hang indefinitely rather than failing fast,
  unlike a plain non-looping decode of the same bytes, so this check
  catches corrupt/unsupported media quickly and reports it as
  `EditorInputError` instead of only surfacing after the full timeout
  elapses.
- **Output:** the final rendered video `assets` row (resolution,
  duration, and the `profile_name` used, in `Asset.metadata_`)
  referenced by a `renders` row.
- **Failure mode:** a compositor crash fails the whole Video Agent job,
  which the Manager retries from the start — renders are not resumable,
  and neither is the module sequence. A missing (but optional) intro/
  outro/music-bed branding asset is not treated as a failure: it's
  logged and skipped, since an accessory clip going missing shouldn't
  abort an otherwise-successful video. A missing *required* asset
  (a segment's own visual/narration/supplementary audio) is a genuine
  data-integrity failure and fails the job honestly, attributed to the
  specific segment/asset.

### Thumbnail Agent

A genuine, independent agent
(`services/agent_video/app/thumbnail_agent.py`'s `ThumbnailAgent`, a real
`BaseAgent` subclass, `name = "thumbnail"`) — not just another pipeline
module — invoked last inside `VideoAgent.run()`, alongside (not part of)
the six-module pipeline above: it has no real ordering dependency on any
of it, since it only needs the finished script and channel branding, not
anything Rendering produces. Runs after Rendering anyway to keep
`VideoAgent.run()` one simple sequence.

- **Input:** `ProjectContext` (`libs.context.build_project_context`) for
  the channel profile (niche, persona, banned topics, style guide) and
  research summary (the video's title/target audience/suggested angle),
  plus the project's full script (read directly — `ProjectContext`
  deliberately carries no script content, see libs/context/schema.py).
- **Does, in order:**
  1. Calls `ThumbnailConceptGenerator` (thumbnail_concept_generator.py) —
     a forced-tool-use Claude call, same pattern as the Research Agent's
     `IdeaGenerator`/the Script Agent's `ScriptGenerator` — analyzing the
     topic, title, full script, and branding to propose
     `THUMBNAIL_CONCEPT_COUNT` (default 3) ranked thumbnail concepts,
     best-first: a concept name, a visual description, an
     image-generation prompt optimized for a 16:9 frame, a short
     on-thumbnail overlay text, and a rationale. Prompts load from
     `prompts/thumbnail/generate_concepts_{system,user}/`, versioned and
     pinnable via `THUMBNAIL_PROMPT_VERSION` — never embedded as Python
     string constants.
  2. Renders the top `THUMBNAIL_RENDER_COUNT` (default 1) concepts into
     real images: checks the Asset Cache for each concept's exact
     `image_prompt`, and on a miss calls
     `libs.providers.get_provider("image_gen")` — the *only* capability
     this agent calls for image generation, independent of Asset
     Generation's own use of the same capability for storyboard shots.
  3. Enforces exactly 1280x720 regardless of whatever native size/aspect
     ratio the provider returned — crop-to-cover (centered, never
     letterboxed) then resize — composites the concept's overlay text
     (falling back to the video title if the concept proposed none)
     locally with Pillow (not a provider capability, the same way
     Rendering's ffmpeg compositing isn't one either), and always emits
     PNG.
  4. Persists one `assets` row per rendered variant, with a
     self-contained metadata record (`prompt`, `provider`, `model` — null
     today, since no `image_gen` provider surfaces one back, unlike TTS's
     `SynthesisResult` — `generation_settings`, `concept_name`,
     `visual_description`, `overlay_text`, `rationale`, `variation`,
     `generated_at`), and one `thumbnails` row per variant
     (`variant_label`, `is_selected` — true only for the best-ranked/first
     variant).
- **Output:** the selected variant's asset id/storage path/concept name
  at the top level, plus every rendered variant, for future A/B variant
  testing (Phase 4, §6) to build on without a schema change —
  `thumbnails`/`is_selected` already support more than one row per
  project today.
- **Failure mode:** a concept-generation or image-gen provider failure
  fails the job honestly, same as every other provider call in this
  pipeline — no template-only fallback, no fabricated concept.
- **Reuse, not reinvention:** Provider Registry (`get_provider`), Asset
  Cache (`services/agent_video/app/asset_cache.py` — shared with Asset
  Generation/Voice Generation), Prompt Management
  (`prompts/thumbnail/`), and `ProjectContext` all reused as-is; no new
  provider abstraction, no hardcoded provider, no new caching mechanism.
  Concept *reasoning* is a direct `anthropic` SDK call rather than a
  `libs.providers` capability — matching every other agent's own
  LLM-reasoning step in this codebase (Research/Script/Manager) — since
  "reuse the Provider Registry"/"don't hardcode providers" is about the
  swappable *image generation* provider, which this agent never hardcodes.
- **Deployment:** deliberately *not* its own `ProjectStage`/Manager-
  dispatched stage — the Manager's workflow plan stays fixed at five
  stages (services/orchestrator/app/manager/workflow.py); promoting this
  to a sixth would mean tracking two jobs per `video_creation` stage, a
  materially bigger change than this agent's own scope. It is invoked
  *in-process* by `VideoAgent.run()` (so the Manager still sees exactly
  one job for the whole stage), but is also independently dispatchable —
  `agents.thumbnail.run` (services/agent_video/app/worker.py), same
  queue/worker, for regenerating a thumbnail without rerunning the whole
  video pipeline. That standalone path sets `reports_to_manager = False`:
  a regeneration job's project can be in *any* stage, and
  `ManagerAgent.handle_job_finished` decides from the project's current
  stage, not the reporting job's `agent_name` — letting a stray thumbnail
  job notify the Manager could incorrectly advance/retry/escalate
  whatever stage that project actually happens to be in (same reasoning
  as the Analytics Agent, §3.10).
- **Prompts:** `prompts/thumbnail/generate_concepts_system/`,
  `prompts/thumbnail/generate_concepts_user/`.

## 3.8 Quality Control Agent

One agent, one queue (`qa`), one `ProjectStage` (`QA_REVIEW`) —
`QualityControlAgent` (services/agent_qa/app/quality_control_agent.py)
inspects a project's entire finished production and decides whether it's
ready to publish. It coordinates five specialized, independent
reviewers (services/agent_qa/app/reviewers/) rather than putting all
evaluation logic in one class — each reviewer owns exactly one category
and knows nothing about any other reviewer or about persistence:

| Reviewer | Category | Deterministic checks | LLM-judged checks |
|---|---|---|---|
| `ScriptReviewer` | script | completeness (has a hook + an ending/CTA), exact-duplicate repetition | factual consistency, quality, engagement, non-exact repetition, grammar, alignment with research |
| `VideoReviewer` | video | missing assets, incorrect scene order, rendering problems (corrupt decode, black frames), video duration | visual consistency and transition/pacing appropriateness (text-only — no video-frame vision capability is wired in) |
| `AudioReviewer` | audio | missing narration, clipping, silence, timing mismatches, synchronization — all measured directly against the real render's audio track | *(none — every check here is objectively measurable; see the module's own docstring for why that's a deliberate choice)* |
| `SubtitleReviewer` | subtitles | timing, readability, overlap, missing captions — re-derived from persisted word timings, since burned-in subtitles are never themselves persisted (see below) | *(none, same reasoning as AudioReviewer)* |
| `ThumbnailReviewer` | thumbnail | *(none)* | readability, title/text visibility, branding consistency, click potential — the **only** reviewer that sends the actual rendered image (not just its generation metadata) to the model, a real multimodal review |

- **Input:** gathered once by `QualityControlAgent._gather` and handed
  to every reviewer as one immutable `ReviewInput`
  (services/agent_qa/app/qa_schema.py) — project metadata and research
  summary via `ProjectContext` (`libs.context.build_project_context`);
  the script and every segment's production metadata, resolved
  storyboard shots, and voiceover (with word timings) via direct
  `Script`/`ScriptSegment`/`StoryboardShot`/`Voiceover`/`Asset` queries
  (script content isn't part of `ProjectContext` — it predates any
  script existing for most of that object's other consumers, see
  libs/context/schema.py); the render and selected thumbnail, downloaded
  from storage once (the render to a temp file for
  `VideoReviewer`/`AudioReviewer`'s ffprobe/ffmpeg commands, the
  thumbnail into memory for `ThumbnailReviewer`'s vision call).
  Subtitle cues are never persisted (the Video Agent's Subtitle
  Generation module computes them in-memory and burns them straight into
  the render — see that module's own docstring), so `SubtitleReviewer`
  re-derives them from the same already-persisted word timings using an
  identical, deliberately duplicated chunking rule (a small, bounded
  duplication across the services/agent_qa ↔ services/agent_video
  container boundary — see that reviewer's own docstring for why that's
  the right tradeoff over a cross-service import).
- **Does:** runs every reviewer against the same `ReviewInput`, then
  aggregates their independent verdicts — a category fails if any of its
  issues is `high` severity (`qa_schema.build_review_result`); the whole
  project is `APPROVED` only if every category passes, else `REJECTED`.
- **Output:** exactly one of `APPROVED`/`REJECTED`, plus every issue
  (grouped by `category`, each with a `severity`, a `detail`, and a
  concrete `suggested_fix`) and per-reviewer metadata (summary, pass/
  fail, issue count), persisted as one `qa_reports` row.
- **Failure mode:** on reject, the report's per-issue `category`
  determines which upstream stage the Manager routes back to (e.g.
  audio/video/subtitles/thumbnail issues → `VIDEO_CREATION`, since those
  are now internal to that one stage rather than separately addressable;
  script issues → `SCRIPTING`), bounded by the project's `retry_count`. A
  reviewer's own LLM call failing (a bad API key, a refusal, a malformed
  response) is never caught and turned into a fabricated "no issues
  found" — it propagates as a genuine job failure, the same
  no-fallback rule every other LLM-backed generator in this codebase
  follows (see llm_review.py's own docstring).
- **Prompts:** `prompts/qa/script_review_{system,user}/`,
  `prompts/qa/video_review_{system,user}/`,
  `prompts/qa/thumbnail_review_{system,user}/` — each versioned and
  pinnable via `QA_PROMPT_VERSION`. `prompts/qa/policy_review/` remains
  an unwired draft (an LLM-based content-policy review against YouTube
  monetization/community guidelines) — a natural sixth reviewer to add
  later; see "Future compatibility" below for why that's a small,
  additive change.

**Reuse, not reinvention.** Every reviewer's LLM call goes through
`libs.providers.get_provider("llm")` (libs/providers/llm/) — the first
consumer in this codebase to route LLM reasoning through the Provider
Registry rather than a direct `anthropic` SDK import (Research/Script/
the Thumbnail Agent's own generators predate this capability and are
unaffected). `LLMProvider.generate_tool_call(system_prompt, user_prompt,
tool, images=None)` forces a structured "report issues" tool call and
returns the model's own name/token counts, so a caller never needs
vendor-specific knowledge; `images` (PNG bytes) is what makes
`ThumbnailReviewer`'s real vision review possible. `config/providers.yaml`
defaults `llm.active` to `anthropic` (unlike every other capability's
`stub` default) — Anthropic is already a hard requirement elsewhere in
this system, so this doesn't add a new paid-vendor dependency, it just
makes an already-required one swappable through this same mechanism.
Every LLM call is wrapped individually in `libs.llm_usage.track_llm_call`
(`llm_review.py`), so the usage log shows exactly which reviewer made
each request. `services/agent_qa/app/media_inspection.py` holds the
shared ffprobe/ffmpeg subprocess plumbing `VideoReviewer`/`AudioReviewer`
both need (probing streams, a decode check, silence/black-frame/peak-
level detection) — real technical checks, not heuristics, each verified
directly against known-good and deliberately-broken test media before
being trusted in a reviewer.

**Future compatibility.** `QualityControlAgent.__init__` holds
`self._reviewers` as a plain list — adding a sixth reviewer (e.g. wiring
up the existing `policy_review` draft) means writing one new class
implementing `reviewers/base.py`'s `Reviewer` interface
(`category: str`, `review(ReviewInput) -> ReviewResult`) and appending an
instance to that list. Nothing about `_gather`, the aggregation rule, or
any existing reviewer needs to change — a new reviewer just reads
whatever fields of the already-gathered `ReviewInput` it needs.

## 3.9 Publisher Agent

One agent, one queue (`publish`), one `ProjectStage` (`PUBLISHING`) —
`PublisherAgent` (services/agent_publisher/app/publisher_agent.py)
refines a project's publish metadata, uploads the finished video and its
selected thumbnail to YouTube through the `youtube` Provider Registry
capability, verifies the upload actually succeeded, and persists
everything onto that project's `publications` row.

- **Input:** gathered once by `PublisherAgent._gather` — the channel
  profile and research summary via `ProjectContext`
  (`libs.context.build_project_context`); the latest `Script`'s content,
  the latest `Render` asset, and the `is_selected` `Thumbnail` asset via
  direct queries (none of this is part of `ProjectContext`, the same
  scoping decision `QualityControlAgent._gather` already makes, §3.8);
  the video and thumbnail bytes themselves, read from storage; and
  whatever `Publication` row already exists for this project, if any
  (the idempotency check below). Missing a render or a selected
  thumbnail fails the job immediately with a `LookupError`, before any
  YouTube API call is attempted — there is nothing to publish without
  both.
- **Does:**
  1. **Refines metadata** via `MetadataGenerator`
     (services/agent_publisher/app/metadata_generator.py) — the agent's
     only LLM call, routed through `libs.providers.get_provider("llm")`
     (the same Provider Registry capability the Quality Control Agent's
     reviewers use, §3.8) rather than a hardcoded `anthropic` import. One
     forced tool call (`refine_publish_metadata`) returns a refined
     title, description, tags, and a playlist *theme* suggestion (e.g.
     "beginner python tutorials") — never a real playlist ID, since the
     model has no way to know which playlists actually exist on the
     channel. A provider failure (bad key, refusal, malformed response)
     is never caught and turned into a fabricated title — it propagates
     as a genuine job failure, the same no-fallback rule every other
     LLM-backed generator in this codebase follows.
  2. **Resolves privacy, scheduling, and playlist** from three sources,
     highest precedence first: an explicit override in the job's
     payload (`privacy_status`, `publish_at`, `playlist_id`) — the hook
     for a human- or Manager-driven one-off decision; the channel's own
     `persona_config` (`default_privacy_status`, `default_playlist_id`,
     a `playlist_theme_map` mapping the LLM's suggested theme to a real
     playlist ID, `youtube_category_id`); finally a safe hardcoded
     default (`"private"`, no playlist). The resolved playlist ID is
     never invented by the `youtube` provider itself — see
     libs/providers/youtube/base.py's `VideoMetadata.playlist_id`.
  3. **Uploads** the video and thumbnail, and adds the video to a
     playlist if one resolved, through whichever `YouTubeProvider` is
     configured (`libs.providers.get_provider("youtube")`) — never a
     hardcoded vendor class. `config/providers.yaml`'s
     `youtube_data_api` entry (`libs/providers/youtube/youtube_data_api_provider.py`)
     is the real, working adapter against the YouTube Data API v3:
     OAuth2 refresh-token exchange (access tokens cached in memory for
     this process only, never written to disk/DB/logs — only the
     refresh token, read from configuration/environment, is a durable
     secret), the Data API's real resumable-upload protocol (resuming
     from the exact byte offset YouTube reports it actually received on
     a network interruption, not a blind full-file retry), and typed
     errors distinguishing auth failures, quota exhaustion (`403`
     `quotaExceeded`/`dailyLimitExceeded`/`rateLimitExceeded` — a "come
     back on a longer horizon" signal, never retried in-process), and
     every other upload failure. `youtube.active` stays `stub` by
     default (unlike `llm`/`editor`) — publishing to a real channel is a
     consequential, hard-to-reverse *public* action, so it requires
     deliberately configuring real OAuth credentials
     (`YOUTUBE_OAUTH_CLIENT_ID`/`_CLIENT_SECRET`/`_REFRESH_TOKEN`) first.
     `dry_run` (a provider-level default, overridable per job via the
     payload) makes every provider call a no-op that returns a
     synthetic result, for testing the whole pipeline without any real
     upload.
  4. **Verifies** the upload via `verify_upload` — a fresh read of the
     video's own `status.uploadStatus`/`privacyStatus` from YouTube,
     never trusting `upload_video`'s own return value as the last word.
     A project is only ever marked published *after* this call reports
     a healthy status; `"failed"`/`"rejected"` fails the job exactly
     like any other provider error.
- **Output:** one `publications` row per project (`libs/models/publication.py`)
  with every field this stage is responsible for: `youtube_video_id`,
  `publish_status` (`SCHEDULED`/`PUBLISHED`/`FAILED`), `privacy_status`,
  `youtube_channel_id` and `url` (both self-reported by the API, never
  assumed), `scheduled_at`/`published_at`, `uploaded_at` (this agent's
  own wall-clock time, distinct from YouTube's own possibly-future
  `published_at`), and the `title`/`description`/`tags`/`playlist_id`
  actually used.
- **Failure mode — idempotent retry, not re-upload:** every project has
  at most one `Publication` row. If a prior, partially-completed attempt
  already recorded a `youtube_video_id` (persisted immediately after a
  successful upload, before the thumbnail/playlist/verify steps even
  run), a retried job skips the upload entirely and resumes from
  wherever it left off against that same video — a duplicate publish is
  treated as a correctness bug, not an acceptable retry side effect. The
  playlist addition is persisted the moment it succeeds too, so a later
  step failing (e.g. `verify_upload`) doesn't cause a retry to add the
  video to the same playlist twice. The one residual gap — this process
  crashing in the narrow window between the playlist API call
  succeeding and that immediate DB write — is an accepted, documented
  tradeoff (no distributed transaction across the YouTube API and
  Postgres), not a fixable bug.
- **Prompts:** `prompts/publish/refine_metadata_{system,user}/`.

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
