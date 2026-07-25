"""The Manager Agent — the pipeline's central brain.

See docs/architecture/01-system-architecture.md §1.4 for the design
rationale (why orchestration is centralized here rather than choreographed
by the agents themselves). This package is that Orchestrator, made
concrete:

- `workflow.py` — the fixed Research → Script → Video Creation → Quality
  Check → Publishing → Analytics plan, as an ordered list of stages.
- `dispatcher.py` — how the Manager hands a stage to a worker agent
  (a `jobs` row + a Celery message).
- `reasoning.py` — the Claude-backed engine that decides
  advance/retry/escalate/abort at every stage transition.
- `manager.py` — `ManagerAgent`, which ties the above together: receiving
  goals, tracking workflow state on `projects`, and reacting to agents
  reporting back.
- `tasks.py` — the Celery task (`manager.advance_workflow`) that is the
  receiving end of every agent's completion notification.
"""

from .manager import ManagerAgent

__all__ = ["ManagerAgent"]
