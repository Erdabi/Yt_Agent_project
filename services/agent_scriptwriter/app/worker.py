"""Script Writing Agent worker.

Registers the Celery task for the `script` queue. The agent's actual work
— generating a full script from an approved idea via an LLM call,
breaking it into timed segments, an optional fact-check pass — is not
implemented yet. `run()` raises `NotImplementedError` rather than
returning a fabricated script; see
docs/architecture/03-agent-responsibilities.md §3.3 and
docs/architecture/06-roadmap.md, Phase 1.

Once implemented, load everything this agent needs through the Project
Context Builder instead of separately querying the channel, project,
idea, and knowledge package:

    from libs.context import build_project_context

    context = build_project_context(project.id, consumer_prompt=("script", "generate_script"))
    # context.channel, context.project, context.research,
    # context.knowledge_package, context.prompt_version, context.manager

`context.prompt_version` is already the concrete resolved version (e.g.
"v1") for prompts/script/generate_script/ — load the template itself via
`get_prompt_loader().get("script", "generate_script", version=context.prompt_version)`.
`context.knowledge_package` is the Research Agent's deep-research output
for the approved idea (verified facts, timeline, entities, citations,
keywords, related topics, hooks, supporting notes — see
libs/schemas/knowledge.py) — write from it directly instead of
re-researching the topic.

Wrap the Claude call itself in `libs.llm_usage.track_llm_call(...)`, the
same way services/orchestrator/app/manager/reasoning.py and
services/agent_research/app/idea_generator.py do, so this agent's usage
is tracked from day one rather than retrofitted later.
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
