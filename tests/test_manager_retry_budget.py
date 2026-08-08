"""The Manager's retry-budget safety nets.

`ManagerAgent.handle_job_finished()` asks an LLM reasoning engine what to
do after each stage, which is genuinely the right call for *judgment*
("is this failure worth another attempt?"). It is the wrong call for
*arithmetic* ("is the budget spent?"), and the engine is handed
`retry_count`/`max_retries` as context it can reason about wrongly.

Both directions of that mistake are real. The ceiling — honoring a
`retry` past the limit — was already guarded. The floor was not, and it
cost a production run: a first-attempt scripting failure was escalated to
human review with the reasoning "this project has reached its retry
limit", when 0 of 3 retries had been used. Escalation means *stop
automation and require a person*, so that decision spent a human's
attention while the project's own budget sat untouched.

These tests pin both nets against the reasoning engine, not around it —
the engine is stubbed to return a specific decision so the assertion is
about what the Manager does with it.
"""

from unittest import mock

import pytest

from libs.models.enums import JobStatus, ProjectStage
from services.orchestrator.app.manager.manager import ManagerAgent
from services.orchestrator.app.manager.reasoning import Decision, WorkflowAction


@pytest.fixture
def manager_with_decision():
    """Builds a ManagerAgent whose reasoning engine returns `decision`,
    with the DB reads it performs faked out, and reports back which
    action was actually applied.
    """

    def _build(decision: Decision, *, job_status: JobStatus, retry_count: int, max_retries: int = 3):
        agent = ManagerAgent()
        agent._reasoning = mock.Mock()
        agent._reasoning.decide.return_value = decision

        applied: dict = {}
        agent._apply = lambda project_id, action, upcoming: applied.update(action=action)
        agent._record_event = lambda *a, **k: None

        job = mock.Mock(
            project_id="11111111-1111-1111-1111-111111111111",
            status=job_status,
            error="something went wrong",
            agent_name="script",
        )
        project = mock.Mock(
            id="11111111-1111-1111-1111-111111111111",
            current_stage=ProjectStage.SCRIPTING,
            retry_count=retry_count,
        )

        session = mock.MagicMock()
        session.get.side_effect = lambda model, _id: (
            job if model.__name__ == "Job" else project
        )
        scope = mock.MagicMock()
        scope.__enter__.return_value = session

        settings = mock.Mock(job_max_retries=max_retries)
        with (
            mock.patch(
                "services.orchestrator.app.manager.manager.sync_session_scope", return_value=scope
            ),
            mock.patch(
                "services.orchestrator.app.manager.manager.get_settings", return_value=settings
            ),
        ):
            agent.handle_job_finished("22222222-2222-2222-2222-222222222222")
        return applied.get("action"), agent

    return _build


def test_escalation_is_overridden_while_retry_budget_remains(manager_with_decision):
    """The regression. The engine escalates a failed job on a false
    premise; the Manager must retry, because the budget is the code's
    fact to know.
    """
    action, _ = manager_with_decision(
        Decision(
            action=WorkflowAction.ESCALATE,
            reasoning="This project has reached its retry limit and no further retries are allowed.",
        ),
        job_status=JobStatus.FAILED,
        retry_count=0,
    )
    assert action == WorkflowAction.RETRY


def test_escalation_stands_once_the_budget_is_actually_spent(manager_with_decision):
    """The override must not become an infinite loop — at the ceiling,
    escalation is the correct and final answer.
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ESCALATE, reasoning="Out of attempts."),
        job_status=JobStatus.FAILED,
        retry_count=3,
        max_retries=3,
    )
    assert action == WorkflowAction.ESCALATE


def test_retry_is_overridden_once_the_budget_is_spent(manager_with_decision):
    """The pre-existing ceiling, kept under test so making the floor
    symmetric didn't quietly break it.
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.RETRY, reasoning="Worth another go."),
        job_status=JobStatus.FAILED,
        retry_count=3,
        max_retries=3,
    )
    assert action == WorkflowAction.ESCALATE


def test_escalation_on_a_succeeded_job_is_never_overridden(manager_with_decision):
    """A job can succeed and still warrant escalation — a QA verdict of
    "rejected" is the case that matters. Retrying a stage that did not
    fail would re-run work for no reason, so the override must key on
    failure, not on budget alone.
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ESCALATE, reasoning="QA rejected the render."),
        job_status=JobStatus.SUCCEEDED,
        retry_count=0,
    )
    assert action == WorkflowAction.ESCALATE


def test_abort_is_never_converted_into_a_retry(manager_with_decision):
    """`abort` is a stronger, distinct signal from `escalate` — "this
    can never work", not "a human should look". The budget nets must
    leave it alone in both directions.
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ABORT, reasoning="Channel was deleted."),
        job_status=JobStatus.FAILED,
        retry_count=0,
    )
    assert action == WorkflowAction.ABORT


def test_advance_is_never_converted(manager_with_decision):
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ADVANCE, reasoning="Stage completed."),
        job_status=JobStatus.SUCCEEDED,
        retry_count=0,
    )
    assert action == WorkflowAction.ADVANCE


# --- the advance guard ------------------------------------------------------
#
# The third net, guarding the pipeline's most basic invariant: a stage is
# only left behind once its job actually succeeded. This one also cost a
# real run — the engine answered `advance` for a scripting job that was
# still RUNNING ("reprocessing will ensure consistency"), so the Video
# Agent started with nothing to work from and failed with "no script
# found for project ...", a message that reads like data corruption and
# is really just the next stage starting too early.


def test_advance_is_refused_while_the_job_is_still_running(manager_with_decision):
    """The regression. A running job needs no decision at all: whatever
    finishes it will notify the Manager again, so acting now would
    either duplicate that work or race it.
    """
    action, _ = manager_with_decision(
        Decision(
            action=WorkflowAction.ADVANCE,
            reasoning="Reprocessing will ensure consistency in the narrative.",
        ),
        job_status=JobStatus.RUNNING,
        retry_count=0,
    )
    assert action is None, "the Manager acted on a job that had not finished"


def test_advance_on_a_failed_job_becomes_a_retry_while_budget_remains(manager_with_decision):
    """Advancing past a failure is never right. With attempts left, the
    useful reading of "advance" is "this stage still needs to happen".
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ADVANCE, reasoning="Not a severe error; move on."),
        job_status=JobStatus.FAILED,
        retry_count=0,
    )
    assert action == WorkflowAction.RETRY


def test_advance_on_a_failed_job_escalates_once_the_budget_is_spent(manager_with_decision):
    """...and at the ceiling it must reach a human rather than loop, so
    the guard cannot become a way around the retry limit.
    """
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ADVANCE, reasoning="Not a severe error; move on."),
        job_status=JobStatus.FAILED,
        retry_count=3,
        max_retries=3,
    )
    assert action == WorkflowAction.ESCALATE


def test_a_succeeded_job_still_advances_untouched(manager_with_decision):
    """The guard must not block the normal path it exists to protect."""
    action, _ = manager_with_decision(
        Decision(action=WorkflowAction.ADVANCE, reasoning="Stage completed."),
        job_status=JobStatus.SUCCEEDED,
        retry_count=0,
    )
    assert action == WorkflowAction.ADVANCE
