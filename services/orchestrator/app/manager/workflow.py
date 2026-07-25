"""The fixed pipeline plan the Manager Agent drives:

    Research -> Script -> Video -> Quality Check -> Publishing

Five high-level stages, no more. Storyboard, voice-over, video assembly,
and thumbnail generation used to each be their own stage here; they are
now internal modules of a single Video Agent
(services/agent_video/app/video_agent.py), invoked as one job on one
queue. The Manager never sees those four sub-steps individually — it
retries or escalates "video" as a whole, not a sub-module of it (see
`services/agent_video/app/video_agent.py` for why that's an acceptable
trade: assembly already depends on both storyboard and voiceover output,
so a partial-video retry was never really "resume from where it broke"
anyway).

Analytics is likewise not in this table at all. It runs as an
independent, recurring service against already-`PUBLISHED` projects (see
services/agent_analytics), not as a step the Manager dispatches or gates
anything on — a failed analytics pull must never be able to affect a
video's production status.

`phase` is what a human reads, `stage` is the fine-grained `ProjectStage`
value the schema and every other agent-facing document already uses
(docs/architecture/01-system-architecture.md §1.3).

This table answers "given where a project is, what runs next" — a plain
lookup. It is deliberately *not* where the "should we actually advance"
judgment call lives; that's the reasoning engine's job (reasoning.py). The
plan is the map; the reasoning engine decides whether and when to follow it.
"""

from dataclasses import dataclass

from libs.models.enums import ProjectStage


@dataclass(frozen=True)
class WorkflowStep:
    phase: str
    stage: ProjectStage
    queue: str
    task_name: str


WORKFLOW: list[WorkflowStep] = [
    WorkflowStep("research", ProjectStage.IDEATION, "research", "agents.research.run"),
    WorkflowStep("script", ProjectStage.SCRIPTING, "script", "agents.script.run"),
    WorkflowStep("video", ProjectStage.VIDEO_CREATION, "video", "agents.video.run"),
    WorkflowStep("quality_check", ProjectStage.QA_REVIEW, "qa", "agents.qa.run"),
    WorkflowStep("publishing", ProjectStage.PUBLISHING, "publish", "agents.publish.run"),
]

_STAGE_INDEX: dict[ProjectStage, int] = {step.stage: i for i, step in enumerate(WORKFLOW)}


def first_step() -> WorkflowStep:
    return WORKFLOW[0]


def step_for_stage(stage: ProjectStage) -> WorkflowStep:
    """The step a project's `current_stage` currently corresponds to."""
    return WORKFLOW[_STAGE_INDEX[stage]]


def next_step(stage: ProjectStage) -> WorkflowStep | None:
    """The step after `stage`, or `None` if `stage` is the last one
    (publishing) — the signal that a project is done, not that something
    is wrong. The Manager marks the project `PUBLISHED`/`COMPLETED` in
    that case rather than dispatching anything further (see
    `ManagerAgent._complete`); analytics is not part of this sequence.
    """
    index = _STAGE_INDEX[stage] + 1
    return WORKFLOW[index] if index < len(WORKFLOW) else None
