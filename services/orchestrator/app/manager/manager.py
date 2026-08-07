"""`ManagerAgent` — the pipeline's central brain.

Owns everything docs/architecture/01-system-architecture.md §1.4 assigns to
the Orchestrator: receiving goals, deciding which agent runs next,
maintaining workflow state on `projects`, and handling failures — by
reacting to worker agents reporting back, never by calling them directly
or being called by them synchronously.
"""

from uuid import UUID

from libs.core.config import get_settings
from libs.core.db import sync_session_scope
from libs.core.logging import bind_job_context, clear_job_context, get_logger
from libs.models.channel import Channel
from libs.models.enums import IdeaStatus, JobStatus, ProjectStage, ProjectStatus
from libs.models.idea import VideoIdea
from libs.models.job import Job
from libs.models.project import Project
from libs.models.system import SystemEvent

from .dispatcher import dispatch_step
from .reasoning import Decision, ReasoningEngine, WorkflowAction
from .workflow import WorkflowStep, first_step, next_step, step_for_stage

logger = get_logger(__name__)


class ManagerAgent:
    def __init__(self, reasoning_engine: ReasoningEngine | None = None) -> None:
        self._reasoning = reasoning_engine or ReasoningEngine()

    # --- Receive goals ---------------------------------------------------

    def receive_goal(self, channel_id: str, goal: str) -> str:
        """Turn a goal into a project and dispatch its first stage
        (research). Research is still asked to run even though a goal
        already states a topic — its job is to turn that raw ask into a
        properly scored, keyworded idea, exactly as it would for a
        self-generated one; the Manager doesn't shortcut that step just
        because a human supplied the starting point.
        """
        with sync_session_scope() as session:
            channel = session.get(Channel, UUID(channel_id))
            if channel is None:
                raise LookupError(f"channel {channel_id} not found")

            idea = VideoIdea(
                channel_id=channel.id,
                title=goal,
                source="goal",
                status=IdeaStatus.APPROVED,
            )
            session.add(idea)
            session.flush()

            project = Project(
                channel_id=channel.id,
                idea_id=idea.id,
                status=ProjectStatus.IN_PROGRESS,
            )
            session.add(project)
            session.flush()
            project_id = str(project.id)

        self._record_event("project", project_id, "goal_received", {"goal": goal})
        dispatch_step(project_id, first_step(), {"goal": goal})
        return project_id

    # --- React to an agent reporting back --------------------------------

    def handle_job_finished(self, job_id: str) -> None:
        """Entry point for the `manager.advance_workflow` Celery task
        (tasks.py) — called once an agent's job has reached a terminal
        state and reported back (see libs/agents/base.py).
        """
        with sync_session_scope() as session:
            job = session.get(Job, job_id)
            if job is None or job.project_id is None:
                return
            project = session.get(Project, job.project_id)
            if project is None:
                return
            job_status = job.status
            error = job.error
            current_stage = project.current_stage
            retry_count = project.retry_count
            project_id = str(project.id)
            agent_name = job.agent_name

        bind_job_context(job_id=job_id, project_id=project_id, agent_name=agent_name)
        try:
            step = step_for_stage(current_stage)
            upcoming = next_step(current_stage)
            max_retries = get_settings().job_max_retries

            decision = self._reasoning.decide(
                project_id=project_id,
                phase=step.phase,
                stage=current_stage.value,
                job_status=job_status.value,
                error=error,
                retry_count=retry_count,
                max_retries=max_retries,
                next_stage=upcoming.stage.value if upcoming else None,
            )

            # Hard safety net: never trust the reasoning engine's own
            # arithmetic for a decision this consequential — enforce the
            # retry ceiling regardless of what it answered.
            if decision.action == WorkflowAction.RETRY and retry_count >= max_retries:
                decision = Decision(
                    action=WorkflowAction.ESCALATE,
                    reasoning=(
                        f"Retry limit ({max_retries}) already reached; "
                        f"escalating instead of honoring a retry decision "
                        f"(reasoning engine said: {decision.reasoning})"
                    ),
                )

            # The same net, in the other direction. The engine is asked
            # for judgment — "is this failure worth another attempt?" —
            # but it is handed `retry_count`/`max_retries` as context and
            # can reason about them wrongly, which is not a hypothetical:
            # a run escalated a first-attempt scripting failure saying
            # "this project has reached its retry limit" with 0 of 3
            # retries used. Escalation means *stop automation and require
            # a person*, so honoring that spends a human's attention while
            # the project's own budget sits untouched — strictly worse
            # than trying again. Budget arithmetic belongs to the code in
            # both directions; only the judgment belongs to the engine.
            #
            # This deliberately costs a genuinely permanent failure its
            # full retry budget before it reaches a human. That is the
            # right trade: attempts are bounded and cheap, the ceiling
            # above still guarantees termination, and most agent failures
            # here are non-deterministic (a model producing malformed or
            # rule-violating output) — exactly the kind another attempt
            # fixes.
            elif (
                decision.action == WorkflowAction.ESCALATE
                and job_status == JobStatus.FAILED
                and retry_count < max_retries
            ):
                decision = Decision(
                    action=WorkflowAction.RETRY,
                    reasoning=(
                        f"Retry budget remains ({retry_count}/{max_retries} used); "
                        f"retrying instead of honoring an escalation decision "
                        f"(reasoning engine said: {decision.reasoning})"
                    ),
                )

            logger.info(
                "manager_decision",
                action=decision.action,
                stage=current_stage.value,
                job_status=job_status.value,
            )
            self._record_event(
                "project",
                project_id,
                "workflow_decision",
                {
                    "action": decision.action,
                    "reasoning": decision.reasoning,
                    "stage": current_stage.value,
                    "job_status": job_status.value,
                },
            )
            self._apply(project_id, decision.action, upcoming)
        finally:
            clear_job_context()

    # --- Applying a decision ---------------------------------------------

    def _apply(self, project_id: str, action: str, upcoming: WorkflowStep | None) -> None:
        if action == WorkflowAction.ADVANCE:
            if upcoming is None:
                self._complete(project_id)
            else:
                self._advance(project_id, upcoming)
        elif action == WorkflowAction.RETRY:
            self._retry(project_id)
        elif action == WorkflowAction.ESCALATE:
            self._set_status(project_id, ProjectStatus.NEEDS_HUMAN_REVIEW)
        else:
            self._set_status(project_id, ProjectStatus.FAILED)

    def _advance(self, project_id: str, step: WorkflowStep) -> None:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            project.current_stage = step.stage
            project.status = ProjectStatus.IN_PROGRESS
            project.retry_count = 0
        dispatch_step(project_id, step, {})

    def _complete(self, project_id: str) -> None:
        """Publishing was the last step in WORKFLOW and it succeeded.
        Explicitly move `current_stage` to `PUBLISHED` rather than leaving
        it at `PUBLISHING` — this is the pipeline's real terminal marker,
        not just a status flag. Analytics does not run from here: it picks
        up already-`PUBLISHED` projects on its own schedule (see
        services/agent_analytics), so completing the pipeline never
        dispatches anything further.
        """
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            project.current_stage = ProjectStage.PUBLISHED
            project.status = ProjectStatus.COMPLETED

    def _retry(self, project_id: str) -> None:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            project.retry_count += 1
            project.status = ProjectStatus.IN_PROGRESS
            stage = project.current_stage
        step = step_for_stage(stage)
        dispatch_step(project_id, step, {})

    def _set_status(self, project_id: str, status: ProjectStatus) -> None:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            project.status = status

    def _record_event(
        self, entity_type: str, entity_id: str, event_type: str, payload: dict
    ) -> None:
        with sync_session_scope() as session:
            session.add(
                SystemEvent(
                    entity_type=entity_type,
                    entity_id=UUID(entity_id),
                    event_type=event_type,
                    payload=payload,
                )
            )
