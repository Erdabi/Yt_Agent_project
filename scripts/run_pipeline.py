#!/usr/bin/env python3
"""Official CLI entry point: run one full video-generation job through the
real pipeline, start to finish.

Before this script existed, the only way to run a complete generation job
was `POST /goals` against a running Orchestrator, with no single command
that also stood up the prerequisites or reported back where the finished
artifacts landed. This script does not reimplement or bypass any of that —
it makes exactly the same call `POST /goals` makes
(`ManagerAgent.receive_goal()`, services/orchestrator/app/manager/manager.py),
then polls the same `projects`/`jobs` tables every other verification
script in this codebase already reads, using the real Celery workers and
real providers configured via `config/providers.yaml`
(`PROVIDERS_CONFIG_PATH` to point elsewhere). It creates no job, no
project, no artifact through any path other than the Manager/Celery
machinery every other entry point already uses.

What this script does NOT do: run any Celery worker itself. Start
`manager_worker` plus whichever per-queue agent workers the job needs
first (see README.md's "Getting started" section) — this only submits the
goal and watches. It also deliberately never starts (and does not expect
you to have started) a worker for the `publish` queue: `youtube.active`
defaults to `stub`, whose `StubYouTubeProvider` raises
`NotImplementedError` unconditionally regardless of `dry_run`
(libs/providers/youtube/stub_provider.py) — there is no way to make a
real, honest publish attempt succeed without real YouTube OAuth
credentials. Once QA_REVIEW passes, the Manager dispatches a `publish` job
and moves `current_stage` to `PUBLISHING`; with no worker consuming that
queue, it simply sits there. This script treats "reached PUBLISHING (or
further) with a Render and a selected Thumbnail already persisted" as
success — the video is finished. Pass `--run-publish` if you *have*
started a publish worker and a real `youtube_data_api` credential and want
this script to keep waiting for a real publish outcome instead.

Usage (from the repo root, venv activated, exactly like every other
command in README.md):

    PYTHONPATH=.:services/orchestrator python scripts/run_pipeline.py \\
        --channel-name "My Channel" --channel-niche "science education" \\
        --goal "a short video about how engines work"

Run `--help` for every option (persona, banned topics, poll interval,
timeout, export directory, skipping preflight checks).
"""

from __future__ import annotations

import argparse
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from libs.core.config import get_settings
from libs.core.db import sync_session_scope
from libs.models import (
    Asset,
    Channel,
    Job,
    Project,
    QAReport,
    Render,
    Script,
    Thumbnail,
    VideoIdea,
    Voiceover,
)
from libs.models.enums import JobStatus, ProjectStage, ProjectStatus
from libs.providers.base import ProviderReadiness
from libs.providers.capabilities import capability_readiness, fulfillable_asset_types
from libs.providers.registry import get_provider
from libs.schemas.script_production import AssetType
from libs.storage.registry import get_storage_backend

# A project sitting at PUBLISHING (or past it) means every stage this
# script is responsible for — research through QA — already finished;
# see this module's own docstring for why PUBLISHING itself is a success
# state here, not a stage still in flight.
_SUCCESS_STAGES = {ProjectStage.PUBLISHING, ProjectStage.PUBLISHED}
_FAILURE_STATUSES = {ProjectStatus.FAILED, ProjectStatus.NEEDS_HUMAN_REVIEW, ProjectStatus.CANCELLED}

# Which asset types this run can actually fulfil, and why not when it
# can't, both come from libs.providers.capabilities — the same functions
# the Script Agent validates against. This script used to keep its own
# copy of the asset-type -> capability map, which meant the constraint it
# advertised in the prompt and the rule the pipeline actually enforced
# were two separate pieces of knowledge that could disagree.


class PipelineError(RuntimeError):
    """Raised for any real, unrecoverable failure this script detects —
    a preflight check failing, the project reaching a failure status, or
    the poll loop timing out. Never caught and silently downgraded:
    main() lets it propagate to a clear message and a non-zero exit code.
    """


# --- preflight ----------------------------------------------------------


def _check_tcp(host: str, port: int, name: str) -> None:
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        raise PipelineError(f"{name} is not reachable at {host}:{port}: {exc}") from exc


def _check_http(url: str, name: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PipelineError(f"{name} is not reachable at {url}: {exc}") from exc


def run_preflight(settings) -> None:
    """Real connectivity checks against every real dependency this job
    needs, before creating anything — failing fast here with a clear
    message beats discovering an unreachable Ollama/ComfyUI instance
    twenty minutes into a run, buried under a stack trace from deep
    inside some agent's job.
    """
    print("== Preflight checks ==")

    _check_tcp(settings.postgres_host, settings.postgres_port, "PostgreSQL")
    print(f"  PostgreSQL   OK  ({settings.postgres_host}:{settings.postgres_port})")

    _check_tcp(settings.redis_host, settings.redis_port, "Redis")
    print(f"  Redis        OK  ({settings.redis_host}:{settings.redis_port})")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise PipelineError("ffmpeg/ffprobe not found on PATH — required by the editor provider")
    print("  ffmpeg/ffprobe OK (on PATH)")

    llm = get_provider("llm")
    if type(llm).__name__ == "OllamaLLMProvider":
        _check_http(f"{settings.ollama_url}/api/version", "Ollama")
        print(f"  Ollama       OK  ({settings.ollama_url})")
    else:
        print(f"  llm provider is {type(llm).__name__!r} — skipping Ollama-specific check")

    checked_comfyui_urls: set[str] = set()
    for capability in ("image_gen", "video_gen"):
        provider = get_provider(capability)
        if type(provider).__name__.startswith("ComfyUI"):
            base_url = f"{settings.comfyui_url}/system_stats"
            if base_url not in checked_comfyui_urls:
                _check_http(base_url, "ComfyUI")
                print(f"  ComfyUI      OK  ({settings.comfyui_url}) [{capability}]")
                checked_comfyui_urls.add(base_url)

    # Reachable is not the same as usable, so report what each capability
    # says about itself too. This never fails the run: a capability being
    # unavailable is a normal, supported configuration — it just narrows
    # which asset types the Script Agent is allowed to ask for, and the
    # operator should be able to see that narrowing happen rather than
    # infer it later from a script that came back without any b-roll.
    print("  capability readiness (constrains what the script may ask for):")
    for capability, readiness in sorted(capability_readiness().items()):
        if readiness.ready:
            print(f"    {capability:<14} available")
        else:
            print(f"    {capability:<14} UNAVAILABLE — {readiness.reason}")

    print()


def _supported_asset_types(readiness: dict[str, ProviderReadiness] | None = None) -> list[str]:
    """Which script `asset_type`s can actually be fulfilled right now."""
    return sorted(
        asset_type.value for asset_type in fulfillable_asset_types(readiness)
    )


# --- channel + goal -------------------------------------------------------


def _compose_persona(operator_persona: str | None, production_note: str) -> str:
    return f"{operator_persona}\n\n{production_note}" if operator_persona else production_note


def get_or_create_channel(
    *,
    name: str,
    niche: str | None,
    operator_persona: str | None,
    production_note: str,
    banned_topics: list[str],
) -> tuple[str, bool]:
    """Returns `(channel_id, created)`. Reuses an existing channel with
    this exact name if one exists; otherwise creates one exactly the way
    `POST /channels` does (services/orchestrator/app/api/channels.py),
    just via a direct session instead of an HTTP round-trip, since this
    script already talks to the database directly for everything else.

    Reuse refreshes the production-constraint note and nothing else. The
    operator's own persona text is theirs and is never rewritten — it is
    kept verbatim under its own `operator_persona` key precisely so the
    two can be told apart — but the note is not authored content, it is
    a *snapshot of this machine's capabilities* that this script derives
    (see `build_production_constraint_note()`). Snapshots go stale, and
    a stale one is worse than no note at all: a channel first set up
    when a capability was broken would keep telling the Script Agent to
    avoid it forever, and — the way this actually bit — a channel set up
    when a capability was wrongly believed *available* would keep
    requesting assets that cannot be produced, long after the detection
    bug behind it was fixed. Recomputing on every run is what makes the
    note describe the environment the job is about to run in.
    """
    with sync_session_scope() as session:
        existing = session.scalar(select(Channel).where(Channel.name == name))
        if existing is not None:
            persona_config = dict(existing.persona_config or {})
            # Channels created before `operator_persona` was tracked
            # separately have only the merged text, and there is no
            # reliable way to split the note back out of it. Trust this
            # run's flag instead of guessing at the old blob.
            stored_operator_persona = persona_config.get("operator_persona")
            operator_text = (
                operator_persona if operator_persona is not None else stored_operator_persona
            )
            if operator_text:
                persona_config["operator_persona"] = operator_text
            persona_config["persona"] = _compose_persona(operator_text, production_note)
            existing.persona_config = persona_config
            return str(existing.id), False

        persona_config = {"persona": _compose_persona(operator_persona, production_note)}
        if operator_persona:
            persona_config["operator_persona"] = operator_persona
        if banned_topics:
            persona_config["banned_topics"] = banned_topics

        channel = Channel(name=name, niche=niche, persona_config=persona_config)
        session.add(channel)
        session.flush()
        return str(channel.id), True


def build_production_constraint_note(
    readiness: dict[str, ProviderReadiness] | None = None,
) -> str:
    resolved = capability_readiness() if readiness is None else readiness
    supported = sorted(set(_supported_asset_types(resolved)))
    # Named explicitly rather than left implicit: a model told only what
    # it *may* use still reaches for a plausible-sounding neighbour, and
    # deriving the forbidden list from the same readiness answer keeps
    # the two halves of this instruction from ever contradicting each
    # other the way a hand-maintained list of names would.
    unsupported = sorted({asset_type.value for asset_type in AssetType} - set(supported))
    note = (
        "PRODUCTION CONSTRAINT (hard requirement, not a style preference): "
        "this channel's video pipeline can only fulfill these production "
        f"asset_requirements asset_type values today: {', '.join(supported)}. "
        "Every beat's asset_requirements must be drawn only from that list."
    )
    if unsupported:
        note += (
            f" Never propose {', '.join(unsupported)} — no working provider is "
            "configured for those, so using one would fail the video outright, "
            "not just look different."
        )
    return note


def submit_goal(channel_id: str, goal: str) -> str:
    # Imported lazily: this pulls in services/orchestrator/app, which is
    # only importable once PYTHONPATH includes that service directory
    # (see this module's own docstring) — importing it at module load
    # time would turn a missing PYTHONPATH entry into a confusing error
    # before argparse even runs.
    from app.manager.manager import ManagerAgent

    return ManagerAgent().receive_goal(channel_id, goal)


# --- polling --------------------------------------------------------------


def poll_until_done(project_id: str, *, poll_interval_sec: float, timeout_sec: float, run_publish: bool) -> Project:
    print(f"== Polling project {project_id} (Ctrl-C to stop watching; the pipeline keeps running) ==")
    deadline = time.monotonic() + timeout_sec
    seen_job_ids: set = set()
    last_stage: ProjectStage | None = None
    last_status: ProjectStatus | None = None

    while True:
        with sync_session_scope() as session:
            project = session.get(Project, UUID(project_id))
            if project is None:
                raise PipelineError(f"project {project_id} vanished mid-run")
            stage, status, retry_count = project.current_stage, project.status, project.retry_count

            jobs = list(
                session.scalars(
                    select(Job).where(Job.project_id == UUID(project_id)).order_by(Job.created_at)
                )
            )

        if stage != last_stage or status != last_status:
            print(f"  [{_elapsed(deadline, timeout_sec)}] stage={stage.value} status={status.value} retry_count={retry_count}")
            last_stage, last_status = stage, status

        for job in jobs:
            if job.id in seen_job_ids or job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                continue
            seen_job_ids.add(job.id)
            if job.status == JobStatus.FAILED:
                print(f"    ! job FAILED  agent={job.agent_name} attempt={job.attempt_count} error={job.error}")
            else:
                print(f"    - job succeeded  agent={job.agent_name} attempt={job.attempt_count}")

        if status in _FAILURE_STATUSES:
            raise PipelineError(
                f"project {project_id} reached {status.value} at stage {stage.value} — "
                "stopping immediately, per the no-bypass instruction this script follows. "
                "See the job errors printed above for the root cause."
            )

        target_stages = {ProjectStage.PUBLISHED} if run_publish else _SUCCESS_STAGES
        if stage in target_stages:
            with sync_session_scope() as session:
                return session.get(Project, UUID(project_id))

        if time.monotonic() >= deadline:
            raise PipelineError(
                f"timed out after {timeout_sec:.0f}s waiting for project {project_id} "
                f"(still at stage={stage.value} status={status.value})"
            )

        time.sleep(poll_interval_sec)


def _elapsed(deadline: float, timeout_sec: float) -> str:
    remaining = deadline - time.monotonic()
    elapsed = timeout_sec - remaining
    return f"+{elapsed:6.0f}s"


# --- final report ----------------------------------------------------------


def collect_report(project_id: str, export_dir: Path) -> dict:
    export_dir.mkdir(parents=True, exist_ok=True)
    storage = get_storage_backend()
    report: dict = {"project_id": project_id, "warnings": []}

    with sync_session_scope() as session:
        project = session.get(Project, UUID(project_id))
        idea = session.get(VideoIdea, project.idea_id)
        report["idea_title"] = idea.title
        report["suggested_length_sec"] = idea.suggested_length_sec

        script = session.scalar(
            select(Script).where(Script.project_id == UUID(project_id)).order_by(Script.version.desc())
        )
        if script is not None:
            report["script_version"] = script.version
            report["script_word_count"] = script.word_count
            report["script_target_duration_sec"] = script.target_duration_sec
            report["script_segment_count"] = len(script.segments)

        voiceover_count = 0
        if script is not None and script.segments:
            segment_ids = [s.id for s in script.segments]
            voiceover_count = len(
                list(session.scalars(select(Voiceover).where(Voiceover.script_segment_id.in_(segment_ids))))
            )
        report["voiceover_segment_count"] = voiceover_count

        render_row = session.scalar(select(Render).where(Render.project_id == UUID(project_id)))
        if render_row is not None:
            render_asset = session.get(Asset, render_row.asset_id)
            report["render_storage_path"] = render_asset.storage_path
            report["render_resolution"] = render_row.resolution
            report["render_duration_sec"] = (
                float(render_row.duration_sec) if render_row.duration_sec is not None else None
            )
        else:
            report["warnings"].append("no Render row found for this project")

        thumbnail_row = session.scalar(
            select(Thumbnail).where(Thumbnail.project_id == UUID(project_id), Thumbnail.is_selected.is_(True))
        )
        if thumbnail_row is not None:
            thumb_asset = session.get(Asset, thumbnail_row.asset_id)
            report["thumbnail_storage_path"] = thumb_asset.storage_path
        else:
            report["warnings"].append("no selected Thumbnail row found for this project")

        qa_report = session.scalar(
            select(QAReport).where(QAReport.project_id == UUID(project_id)).order_by(QAReport.checked_at.desc())
        )
        if qa_report is not None:
            report["qa_passed"] = qa_report.passed
            report["qa_issue_count"] = len(qa_report.issues)
            if qa_report.issues:
                report["warnings"].append(f"QA reported {len(qa_report.issues)} issue(s) — see qa_reports table")

        failed_jobs = list(
            session.scalars(
                select(Job).where(Job.project_id == UUID(project_id), Job.status == JobStatus.FAILED)
            )
        )
        for job in failed_jobs:
            report["warnings"].append(f"job {job.agent_name} failed at least once: {job.error}")
        retried_jobs = [
            j
            for j in session.scalars(select(Job).where(Job.project_id == UUID(project_id)))
            if j.attempt_count > 1
        ]
        for job in retried_jobs:
            report["warnings"].append(f"job {job.agent_name} needed {job.attempt_count} attempts before succeeding")

    if "render_storage_path" in report:
        dest = export_dir / f"{project_id}_final.mp4"
        dest.write_bytes(storage.read_bytes(report["render_storage_path"]))
        report["exported_mp4_path"] = str(dest)

    if "thumbnail_storage_path" in report:
        suffix = Path(report["thumbnail_storage_path"]).suffix or ".png"
        dest = export_dir / f"{project_id}_thumbnail{suffix}"
        dest.write_bytes(storage.read_bytes(report["thumbnail_storage_path"]))
        report["exported_thumbnail_path"] = str(dest)

    return report


def print_report(report: dict) -> None:
    print("\n== Final report ==")
    print(f"  Project id:            {report['project_id']}")
    print(f"  Idea/topic:            {report.get('idea_title')}")
    print(f"  Suggested length:      {report.get('suggested_length_sec')}s")
    print(f"  Script segments:       {report.get('script_segment_count')}")
    print(f"  Voiceover segments:    {report.get('voiceover_segment_count')}")
    print(f"  QA passed:             {report.get('qa_passed')} ({report.get('qa_issue_count', 0)} issue(s))")
    print(f"  Render duration:       {report.get('render_duration_sec')}s @ {report.get('render_resolution')}")
    print(f"  Final MP4:             {report.get('exported_mp4_path', '<missing>')}")
    print(f"  Thumbnail:             {report.get('exported_thumbnail_path', '<missing>')}")
    if report["warnings"]:
        print(f"  Warnings ({len(report['warnings'])}):")
        for warning in report["warnings"]:
            print(f"    - {warning}")
    else:
        print("  Warnings:              none")


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one full video-generation job through the real pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--goal", required=True, help="The video topic/goal text (what POST /goals's 'goal' field takes).")
    parser.add_argument("--channel-name", required=True, help="Reuse a channel with this exact name, or create one.")
    parser.add_argument("--channel-niche", default=None)
    parser.add_argument(
        "--persona",
        default=None,
        help="Channel tone/persona text. This script always appends a production-constraint note "
        "listing which asset_requirements types are actually fulfillable by the configured providers "
        "(see build_production_constraint_note()) — this flag adds to that, it never replaces it.",
    )
    parser.add_argument("--banned-topic", action="append", default=[], dest="banned_topics")
    parser.add_argument("--poll-interval-sec", type=float, default=5.0)
    parser.add_argument("--timeout-sec", type=float, default=7200.0)
    parser.add_argument("--export-dir", default="data/exports")
    parser.add_argument(
        "--run-publish",
        action="store_true",
        help="Wait for a real PUBLISHED state instead of stopping at PUBLISHING. Only meaningful if "
        "you have already started a 'publish' queue worker AND configured a real youtube_data_api "
        "credential — otherwise this will simply time out, since the stub provider cannot succeed.",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the startup connectivity checks.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()

    try:
        if not args.skip_preflight:
            run_preflight(settings)

        production_note = build_production_constraint_note()

        channel_id, created = get_or_create_channel(
            name=args.channel_name,
            niche=args.channel_niche,
            operator_persona=args.persona,
            production_note=production_note,
            banned_topics=args.banned_topics,
        )
        print(f"== Channel {'created' if created else 'reused'}: {channel_id} ==")
        print(f"  {production_note}")
        if not created:
            print("  (reused channel: the production-constraint note above was refreshed to match")
            print("   this machine; any persona text you set previously was left as-is)")

        project_id = submit_goal(channel_id, args.goal)
        print(f"== Goal submitted, project created: {project_id} ==\n")

        project = poll_until_done(
            project_id,
            poll_interval_sec=args.poll_interval_sec,
            timeout_sec=args.timeout_sec,
            run_publish=args.run_publish,
        )
        print(f"\n== Reached stage={project.current_stage.value} status={project.status.value} ==")

        report = collect_report(project_id, Path(args.export_dir))
        print_report(report)
        return 0

    except PipelineError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
