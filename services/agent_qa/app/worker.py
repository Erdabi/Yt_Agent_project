"""Quality Assurance Agent worker.

Registers the Celery task for the `qa` queue. The agent's actual work —
automated technical checks (sync, silence, duration, loudness) plus an
LLM-based content-policy review — is not implemented yet. `run()` raises
`NotImplementedError` rather than fabricating a pass/fail verdict; see
docs/architecture/03-agent-responsibilities.md §3.8 and
docs/architecture/06-roadmap.md, Phase 2. Its policy-review prompt
already exists at prompts/qa/policy_review/ (see libs/prompts) — load it
via `get_prompt_loader().get("qa", "policy_review")` once this lands.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.core.celery_app import AgentTask, celery_app
from libs.schemas.jobs import JobContext


class QAAgent(BaseAgent):
    name = "qa"

    def run(self, context: JobContext) -> dict[str, Any]:
        raise NotImplementedError(
            "QA agent logic lands in Phase 2 of the roadmap "
            "(docs/architecture/06-roadmap.md) — see "
            "docs/architecture/03-agent-responsibilities.md §3.8."
        )


@celery_app.task(name="agents.qa.run", bind=True, base=AgentTask, queue="qa")
def run_qa_job(self: AgentTask, job_id: str) -> dict[str, Any]:
    return QAAgent().execute_job(job_id)
