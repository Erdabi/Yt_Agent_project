"""Research / Ideation Agent worker.

The Research Agent's only responsibility: find high-potential YouTube
video ideas. It runs in one of two modes, both handled by the same
`run()` method:

- **Enrich mode** (`context.project_id` is set): the Manager dispatched
  this as the `research` stage of an existing project — see
  `ManagerAgent.receive_goal` (services/orchestrator/app/manager/manager.py),
  which already created a `Project` and a `VideoIdea` row (title=goal,
  status=APPROVED) before this job ever ran. This mode elaborates on that
  one goal — trend context, keywords, audience, angle, competition,
  score, suggested length, research notes — and updates the same
  `VideoIdea` row rather than creating a new one.
- **Discover mode** (`context.project_id` is `None`, `payload["channel_id"]`
  set instead): a channel-level ideation run with no project yet —
  generates several new candidate ideas from trend signals/seed topics and
  inserts each as a new `VideoIdea` row (status=PROPOSED, awaiting
  approval). Dispatched via `agents.research.discover` (see the task at
  the bottom of this file), not by the Manager — `Job.project_id` is
  nullable specifically for jobs like this one
  (docs/architecture/04-database-design.md §4.2). Nothing schedules this
  automatically yet (no Celery beat for it, unlike Analytics's
  `agent_analytics_beat`) — it's invoked directly today
  (`celery_app.send_task("agents.research.discover", ...)`), with a
  recurring "daily ideation run per channel" trigger
  (docs/architecture/01-system-architecture.md §1.5, step 1) a natural
  follow-up once there's a consumer for it (a dashboard, an API endpoint).

Both modes share three building blocks:
- `TrendAggregator` (trend_sources/) — trend signals feeding the prompt;
  only the seed-topic source is real today, the rest are honest
  `NotImplementedError` stubs (Phase 4).
- `IdeaGenerator` (idea_generator.py) — the actual Claude call, forced
  tool use, prompts loaded from prompts/research/ via libs/prompts.
- `DuplicateChecker` (dedup.py) — a lexical similarity check against this
  channel's existing ideas; flags a likely duplicate in `research_notes`
  and caps the score rather than failing the job outright.

Enrich mode additionally builds a **Knowledge Package**
(`KnowledgeBuilder`, knowledge_builder.py) — deep, source-grounded
research on the now-finalized topic (verified facts, timeline, entities,
citations, keywords, related topics, hooks, supporting notes; see
libs/schemas/knowledge.py) — and stores it as JSON + Markdown via
`libs.storage`, so the Script Agent can write directly from it instead of
researching the topic itself. Discover mode does not: generating a full
web-search-backed knowledge package for every one of several unapproved
candidate ideas would be expensive and mostly wasted, since most won't be
approved — it's built once, for the one idea a human/goal has actually
committed to.

See docs/architecture/03-agent-responsibilities.md §3.2 for the full
picture, including the `Channel.persona_config` keys this code reads
(`tone`/`persona`, `banned_topics`, `seed_topics`).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.core.db import sync_session_scope
from libs.core.logging import get_logger
from libs.models.channel import Channel
from libs.models.enums import CompetitionLevel, IdeaStatus, JobStatus
from libs.models.idea import VideoIdea
from libs.models.job import Job
from libs.models.project import Project
from libs.schemas.jobs import JobContext
from libs.schemas.knowledge import KnowledgePackage
from libs.storage import get_storage_backend

from .dedup import DuplicateChecker
from .idea_generator import GeneratedIdea, IdeaGenerator
from .knowledge_builder import KnowledgeBuilder
from .knowledge_package_render import render_markdown
from .trend_sources.aggregator import TrendAggregator

logger = get_logger(__name__)

#: Below this lexical similarity, an idea is not treated as a duplicate.
_DUPLICATE_SIMILARITY_THRESHOLD = 0.6
#: A likely-duplicate idea is still stored (a human can judge it), but its
#: confidence score is capped so it doesn't rank above genuinely new ideas.
_DUPLICATE_SCORE_CAP = 40


def _channel_context(channel: Channel) -> tuple[str, str, list[str], list[str]]:
    """Pull the persona fields this agent understands out of a channel's
    freeform `persona_config` JSONB. Returns
    (niche, persona_text, banned_topics, configured_seed_topics).
    """
    persona = channel.persona_config or {}
    niche = channel.niche or "general"
    persona_text = persona.get("tone") or persona.get("persona") or "general audience"
    banned_topics = list(persona.get("banned_topics") or [])
    seed_topics = list(persona.get("seed_topics") or [])
    return niche, persona_text, banned_topics, seed_topics


class ResearchAgent(BaseAgent):
    name = "research"

    def __init__(self) -> None:
        self._trends = TrendAggregator()
        self._generator = IdeaGenerator()
        self._dedup = DuplicateChecker(threshold=_DUPLICATE_SIMILARITY_THRESHOLD)
        self._knowledge = KnowledgeBuilder()
        self._storage = get_storage_backend()

    def run(self, context: JobContext) -> dict[str, Any]:
        if context.project_id:
            return self._enrich_project_idea(context)
        return self._discover_channel_ideas(context)

    # --- Enrich mode: one goal-driven idea, tied to an existing project --

    def _enrich_project_idea(self, context: JobContext) -> dict[str, Any]:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(context.project_id))
            if project is None:
                raise LookupError(f"project {context.project_id} not found")
            idea = session.get(VideoIdea, project.idea_id)
            if idea is None:
                raise LookupError(f"idea {project.idea_id} not found")
            channel = session.get(Channel, project.channel_id)
            if channel is None:
                raise LookupError(f"channel {project.channel_id} not found")

            idea_id = str(idea.id)
            channel_id = str(channel.id)
            goal = context.payload.get("goal") or idea.title
            channel_niche, channel_persona, banned_topics, seed_topics = _channel_context(channel)

            existing = session.scalars(
                select(VideoIdea).where(
                    VideoIdea.channel_id == channel.id, VideoIdea.id != idea.id
                )
            ).all()
            existing_titles = [e.title for e in existing]
            existing_snapshot = [(str(e.id), e.title, list(e.keywords or [])) for e in existing]

        trend_signals = self._trends.gather_signals(
            channel_niche=channel_niche, seed_topics=seed_topics or [goal]
        )
        generated = self._generator.generate(
            channel_niche=channel_niche,
            channel_persona=channel_persona,
            banned_topics=banned_topics,
            existing_titles=existing_titles,
            trend_signals=trend_signals,
            goal=goal,
            count=1,
        )
        idea_data = generated[0]
        research_notes, score, duplicate = self._apply_dedup(idea_data, existing_snapshot)

        package = self._knowledge.build(
            topic=idea_data.topic,
            target_audience=idea_data.target_audience,
            channel_niche=channel_niche,
            channel_persona=channel_persona,
            banned_topics=banned_topics,
            existing_keywords=idea_data.keywords,
        )
        json_path, md_path = self._store_knowledge_package(context.project_id, package)

        with sync_session_scope() as session:
            idea = session.get(VideoIdea, UUID(idea_id))
            self._write_idea_fields(idea, idea_data, research_notes, score)
            idea.knowledge_package_json_path = json_path
            idea.knowledge_package_md_path = md_path

        logger.info(
            "research_idea_enriched",
            idea_id=idea_id,
            channel_id=channel_id,
            possible_duplicate=duplicate is not None,
            knowledge_package_json_path=json_path,
        )
        return self._result(
            mode="enrich", idea_id=idea_id, idea_data=idea_data, research_notes=research_notes,
            score=score, duplicate=duplicate,
            knowledge_package_json_path=json_path, knowledge_package_md_path=md_path,
            knowledge_package_summary=package.summary,
        )

    def _store_knowledge_package(self, project_id: str, package: KnowledgePackage) -> tuple[str, str]:
        json_path = self._storage.save_bytes(
            project_id, "research", "knowledge_package.json",
            package.model_dump_json(indent=2).encode("utf-8"),
        )
        md_path = self._storage.save_bytes(
            project_id, "research", "knowledge_package.md",
            render_markdown(package).encode("utf-8"),
        )
        return json_path, md_path

    # --- Discover mode: several new candidate ideas for a channel --------

    def _discover_channel_ideas(self, context: JobContext) -> dict[str, Any]:
        payload = context.payload
        channel_id = payload.get("channel_id")
        if not channel_id:
            raise ValueError(
                "a research job with no project_id must carry a 'channel_id' in "
                "its payload (dispatched via agents.research.discover)"
            )
        requested_seed_topics = list(payload.get("seed_topics") or [])
        count = int(payload.get("count") or 5)

        with sync_session_scope() as session:
            channel = session.get(Channel, UUID(channel_id))
            if channel is None:
                raise LookupError(f"channel {channel_id} not found")
            channel_niche, channel_persona, banned_topics, configured_seed_topics = _channel_context(channel)

            existing = session.scalars(
                select(VideoIdea).where(VideoIdea.channel_id == channel.id)
            ).all()
            existing_titles = [e.title for e in existing]
            existing_snapshot = [(str(e.id), e.title, list(e.keywords or [])) for e in existing]

        seed_topics = requested_seed_topics + [
            t for t in configured_seed_topics if t not in requested_seed_topics
        ]
        trend_signals = self._trends.gather_signals(
            channel_niche=channel_niche, seed_topics=seed_topics or None
        )
        generated = self._generator.generate(
            channel_niche=channel_niche,
            channel_persona=channel_persona,
            banned_topics=banned_topics,
            existing_titles=existing_titles,
            trend_signals=trend_signals,
            goal=None,
            count=count,
        )

        created: list[dict[str, Any]] = []
        with sync_session_scope() as session:
            for idea_data in generated:
                research_notes, score, duplicate = self._apply_dedup(idea_data, existing_snapshot)
                idea = VideoIdea(channel_id=UUID(channel_id), source="research_agent", status=IdeaStatus.PROPOSED)
                self._write_idea_fields(idea, idea_data, research_notes, score)
                session.add(idea)
                session.flush()
                # so a later idea in this same batch can't "duplicate" an
                # earlier one from the batch either
                existing_snapshot.append((str(idea.id), idea_data.topic, idea_data.keywords))
                created.append(
                    self._result(
                        mode="discover", idea_id=str(idea.id), idea_data=idea_data,
                        research_notes=research_notes, score=score, duplicate=duplicate,
                    )
                )

        logger.info("research_ideas_discovered", channel_id=channel_id, count=len(created))
        return {"mode": "discover", "channel_id": channel_id, "ideas": created}

    # --- Shared helpers ----------------------------------------------------

    def _apply_dedup(
        self, idea_data: GeneratedIdea, existing_snapshot: list[tuple[str, str, list[str]]]
    ):
        duplicate = self._dedup.find_best_match(idea_data.topic, idea_data.keywords, existing_snapshot)
        research_notes = idea_data.research_notes
        score = idea_data.score
        if duplicate is not None:
            research_notes = (
                f'[Possible duplicate: {duplicate.similarity:.0%} similar to existing '
                f'idea {duplicate.idea_id} "{duplicate.title}"] ' + research_notes
            )
            score = min(score, _DUPLICATE_SCORE_CAP)
        return research_notes, score, duplicate

    @staticmethod
    def _write_idea_fields(idea: VideoIdea, idea_data: GeneratedIdea, research_notes: str, score: int) -> None:
        idea.title = idea_data.topic
        idea.keywords = idea_data.keywords
        idea.rationale = idea_data.why_people_would_watch
        idea.score = score
        idea.target_audience = idea_data.target_audience
        idea.suggested_angle = idea_data.suggested_angle
        idea.competition_level = CompetitionLevel(idea_data.competition_level)
        idea.suggested_length_sec = idea_data.suggested_length_sec
        idea.research_notes = research_notes

    @staticmethod
    def _result(
        *, mode: str, idea_id: str, idea_data: GeneratedIdea, research_notes: str, score: int, duplicate,
        knowledge_package_json_path: str | None = None,
        knowledge_package_md_path: str | None = None,
        knowledge_package_summary: str | None = None,
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "idea_id": idea_id,
            "topic": idea_data.topic,
            "target_audience": idea_data.target_audience,
            "why_people_would_watch": idea_data.why_people_would_watch,
            "keywords": idea_data.keywords,
            "suggested_angle": idea_data.suggested_angle,
            "competition_level": idea_data.competition_level,
            "score": score,
            "suggested_length_sec": idea_data.suggested_length_sec,
            "research_notes": research_notes,
            "possible_duplicate_of": duplicate.idea_id if duplicate else None,
            # None in discover mode — see the module docstring for why a
            # Knowledge Package is only built in enrich mode.
            "knowledge_package_json_path": knowledge_package_json_path,
            "knowledge_package_md_path": knowledge_package_md_path,
            "knowledge_package_summary": knowledge_package_summary,
        }


@celery_app.task(name="agents.research.run", bind=True, base=AgentTask, queue="research")
def run_research_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ResearchAgent().execute_job(job_id)


@celery_app.task(name="agents.research.discover", bind=True, base=AgentTask, queue="research")
def discover_ideas_job(
    self: AgentTask,
    channel_id: str,
    seed_topics: list[str] | None = None,
    count: int = 5,
) -> dict[str, Any]:
    """Channel-level entry point — not part of the Manager's pipeline (see
    module docstring). Creates its own `Job` row (`project_id=None`) the
    same way `agents.analytics.sweep` does for its per-publication jobs,
    then runs it through the normal `BaseAgent.execute_job` path so it
    gets the same audit trail as every other agent invocation.
    """
    with sync_session_scope() as session:
        job = Job(
            project_id=None,
            agent_name=ResearchAgent.name,
            queue_name="research",
            status=JobStatus.QUEUED,
            payload={"channel_id": channel_id, "seed_topics": seed_topics, "count": count},
        )
        session.add(job)
        session.flush()
        job_id = str(job.id)

    return ResearchAgent().execute_job(job_id)
