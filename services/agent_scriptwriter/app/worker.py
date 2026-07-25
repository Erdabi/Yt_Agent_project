"""Script Writing Agent worker.

Registers the Celery task for the `script` queue. The agent's actual work
— generating a full script from an approved idea via an LLM call,
breaking it into timed segments, an optional fact-check pass — is not
implemented yet. `run()` raises `NotImplementedError` rather than
returning a fabricated script; see
docs/architecture/03-agent-responsibilities.md §3.3 and
docs/architecture/06-roadmap.md, Phase 1. Its generation prompt already
exists at prompts/script/generate_script/ (see libs/prompts) — load it
via `get_prompt_loader().get("script", "generate_script")` once this lands.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class ScriptwriterAgent(BaseAgent):
    name = "script"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "Script Writing agent logic lands in Phase 1 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.3."
        )


@celery_app.task(name="agents.script.run", bind=True, base=AgentTask, queue="script")
def run_script_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return ScriptwriterAgent().execute_job(job_id)
