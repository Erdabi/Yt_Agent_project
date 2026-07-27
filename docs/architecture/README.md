# Autonomous AI YouTube Video Creation System — Architecture

This directory contains the production architecture for an autonomous, agent-based
YouTube content pipeline: research → script → video (storyboard, voice-over,
assembly, and thumbnail generation as one agent's internal modules) → QA →
publish, deployed as Docker Compose services on a single Hetzner Cloud VPS.
Performance tracking (analytics) runs independently, on its own schedule,
against already-published videos — it is not a step in that pipeline.

These documents are both the original design baseline and the current
reference for the implementation, which now exists and follows it: the
Orchestrator, all six worker agents, the provider layer, and the full
Postgres schema are built and verified end to end (see
[Development Roadmap](./06-roadmap.md) for exactly what's done vs. still
planned per phase — each document above is updated as implementation
work lands, not left to drift from what actually runs).

## Documents

1. [System Architecture](./01-system-architecture.md) — components, data flow, pipeline state machine, deployment topology
2. [Folder Structure](./02-folder-structure.md) — monorepo layout for services, shared libraries, infra
3. [Agent Responsibilities](./03-agent-responsibilities.md) — what each agent owns, its inputs/outputs, and failure modes
4. [Database Design](./04-database-design.md) — PostgreSQL schema, entity relationships, indexing strategy
5. [Technology Choices](./05-technology-choices.md) — stack decisions and rationale, including the provider-swap strategy
6. [Development Roadmap](./06-roadmap.md) — phased delivery plan from foundations to a self-optimizing feedback loop

## Design principles

These recur throughout every document and should guide implementation decisions later:

- **Orchestration is centralized, execution is distributed.** A single Orchestrator
  owns the pipeline state machine; agents are stateless workers that do one job and
  report back. Agents never decide "what happens next" — that avoids the
  spaghetti-choreography failure mode where business logic is scattered across
  services.
- **Every AI capability sits behind a provider abstraction.** LLM, TTS, image
  generation, video generation, stock-footage sourcing, and audio-library sourcing
  are all accessed through an internal interface (`libs/providers/*`). Swapping
  Anthropic for OpenAI, or ElevenLabs for Azure Speech, is a configuration change,
  not a code change — and because the Video Agent's pipeline modules only ever
  pass each other already-resolved assets (never a provider instance), swapping
  one capability's provider never touches another module's code either.
- **Everything that happens is a row in `jobs`.** Every agent invocation is recorded
  with its inputs, outputs, status, and error — this is the audit trail, the retry
  mechanism, and the debugging tool, all at once.
- **Human-in-the-loop gates are configuration, not hardcoded.** Early on, every
  stage can require manual approval; as confidence grows, gates are relaxed via
  config without code changes.
- **The system must survive a VPS reboot.** Single-host deployment means no
  multi-node failover — durability comes from idempotent operations, persisted
  job state, and Compose `restart: unless-stopped` policies, not from redundancy.
