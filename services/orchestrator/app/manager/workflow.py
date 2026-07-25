"""The fixed pipeline plan the Manager Agent drives:

    Research -> Script -> Video Creation -> Quality Check -> Publishing -> Analytics

"Video Creation" is one user-facing phase but four concrete agents
(storyboard, voiceover, assembly, thumbnail) — this table is the ground
truth for both views: `phase` is what a human reads, `stage` is the
fine-grained `ProjectStage` value the schema and every other agent-facing
document already uses (docs/architecture/01-system-architecture.md §1.3).

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
    WorkflowStep("video_creation", ProjectStage.STORYBOARD, "storyboard", "agents.storyboard.run"),
    WorkflowStep("video_creation", ProjectStage.VOICEOVER, "voiceover", "agents.voiceover.run"),
    WorkflowStep("video_creation", ProjectStage.VIDEO_ASSEMBLY, "assembly", "agents.assembly.run"),
    WorkflowStep("video_creation", ProjectStage.THUMBNAIL, "thumbnail", "agents.thumbnail.run"),
    WorkflowStep("quality_check", ProjectStage.QA_REVIEW, "qa", "agents.qa.run"),
    WorkflowStep("publishing", ProjectStage.PUBLISHING, "publish", "agents.publish.run"),
    WorkflowStep("analytics", ProjectStage.ANALYTICS, "analytics", "agents.analytics.run"),
]

_STAGE_INDEX: dict[ProjectStage, int] = {step.stage: i for i, step in enumerate(WORKFLOW)}


def first_step() -> WorkflowStep:
    return WORKFLOW[0]


def step_for_stage(stage: ProjectStage) -> WorkflowStep:
    """The step a project's `current_stage` currently corresponds to."""
    return WORKFLOW[_STAGE_INDEX[stage]]


def next_step(stage: ProjectStage) -> WorkflowStep | None:
    """The step after `stage`, or `None` if `stage` is the last one
    (analytics) — the signal that a project is done, not that something
    is wrong.
    """
    index = _STAGE_INDEX[stage] + 1
    return WORKFLOW[index] if index < len(WORKFLOW) else None
