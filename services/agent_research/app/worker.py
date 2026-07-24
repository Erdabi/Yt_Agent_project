"""Research / Ideation Agent worker.

Registers the Celery task for the `research` queue. The agent's actual
work — pulling trend signals, scoring candidate ideas with an LLM call,
checking for semantic duplication against past videos — is not
implemented yet. `run()` raises `NotImplementedError` rather than
returning fabricated ideas; see
docs/architecture/03-agent-responsibilities.md §3.2 and
docs/architecture/06-roadmap.md, Phase 1.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class ResearchAgent(BaseAgent):
    name = "research"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Research agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.2."
        )


@celery_app.task(name="agents.research.run", bind=True, base=AgentTask, queue="research")
def run_research_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ResearchAgent().execute_job(job_id)
