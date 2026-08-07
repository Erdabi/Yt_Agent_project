#!/usr/bin/env python3
"""Automated acceptance check for a finished video.

Answers, from the rendered MP4 and the project's own database rows,
whether a generated video is actually watchable — not merely whether the
pipeline exited zero. Every check is a measurement, never a self-report:
duration comes from `ffprobe`, image change and motion from decoded
frames, reuse and retries from the rows the pipeline itself wrote.

    PYTHONPATH=. python scripts/acceptance_check.py --project-id <uuid>

Exits non-zero if any check fails, so it works as a gate in a script or
a CI step as well as a human-readable report.

Why frames rather than trusting the plan: Visual Beat Planning can be
correct and the compositor still emit something static or looping, and
that gap is exactly where this pipeline's defects have historically
lived. Reading the finished pixels is the only check that can't be
fooled by a plan that looks right on paper.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from libs.core.db import sync_session_scope
from libs.models import Asset, Job, Project, Render, Script, Voiceover
from libs.models.enums import AssetType as PhysicalAssetType
from libs.storage import get_storage_backend

# --- tunables (all overridable from the command line) ----------------------

#: How closely the rendered video must match the narration it was built
#: from. Sub-second, because anything larger is either truncated speech
#: or footage running past the audio.
DEFAULT_DURATION_TOLERANCE_SEC = 1.0

#: Frames are sampled this often to measure change, motion and sharpness.
#: Fine enough to see inside a 2s visual, coarse enough that a 5-minute
#: video doesn't decode thousands of frames.
DEFAULT_SAMPLE_INTERVAL_SEC = 0.5

#: Mean absolute per-pixel difference (0-255) between two sampled frames,
#: above which they are considered visually different content rather than
#: the same shot. Set well above codec noise and well below a real cut.
DEFAULT_FRAME_CHANGE_THRESHOLD = 12.0

#: Smaller differences than this still count as *motion* within one
#: shot — a Ken Burns push moves the frame slightly every sample without
#: changing what is on screen.
DEFAULT_MOTION_THRESHOLD = 0.8

#: Variance of the Laplacian, the standard focus measure. Below this a
#: frame is blurry or empty rather than a real, detailed image.
DEFAULT_SHARPNESS_THRESHOLD = 15.0

#: The band a visual should hold the screen for. Longer reads as a
#: slideshow; shorter reads as a flicker.
DEFAULT_MIN_AVG_IMAGE_SEC = 1.5
DEFAULT_MAX_AVG_IMAGE_SEC = 6.0


@dataclass
class CheckResult:
    number: int
    name: str
    passed: bool
    detail: str
    #: Set when a check couldn't be evaluated at all (a missing
    #: dependency, an artifact that doesn't exist). Reported distinctly
    #: from a failure: "we didn't measure this" is not "this is fine".
    skipped: bool = False


@dataclass
class Report:
    project_id: str
    results: list[CheckResult] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def add(self, number: int, name: str, passed: bool, detail: str, *, skipped: bool = False) -> None:
        self.results.append(CheckResult(number, name, passed, detail, skipped))

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and not r.skipped]

    @property
    def skipped(self) -> list[CheckResult]:
        return [r for r in self.results if r.skipped]


# --- media probing ----------------------------------------------------------


def _ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode('utf-8', 'replace')}")
    return json.loads(result.stdout)


def _sample_frames(video: Path, interval_sec: float, work_dir: Path) -> list[Path]:
    """Decode one frame every `interval_sec` into `work_dir`, in order."""
    pattern = work_dir / "frame_%05d.png"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video),
         "-vf", f"fps=1/{interval_sec},scale=320:-1", str(pattern)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"frame extraction failed: {result.stderr.decode('utf-8', 'replace')}")
    return sorted(work_dir.glob("frame_*.png"))


def _load_gray(path: Path):
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(image.convert("L"), dtype="float32")


def _mean_abs_diff(first, second) -> float:
    import numpy as np

    return float(np.mean(np.abs(first - second)))


def _laplacian_variance(gray) -> float:
    """Variance of the Laplacian — the standard focus measure. A sharp,
    detailed frame has high edge energy; a blurred or flat one does not.
    """
    import numpy as np

    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype="float32")
    height, width = gray.shape
    if height < 3 or width < 3:
        return 0.0
    windows = np.lib.stride_tricks.sliding_window_view(gray, (3, 3))
    convolved = np.einsum("ijkl,kl->ij", windows, kernel)
    return float(np.var(convolved))


# --- database facts ---------------------------------------------------------


@dataclass
class ProjectFacts:
    narration_total_sec: float
    segment_count: int
    distinct_narration_texts: int
    image_asset_rows: int
    distinct_image_paths: int
    image_paths: list[str]
    render_storage_path: str | None
    render_duration_sec: float | None
    stub_provider_assets: list[str]
    jobs_with_retries: list[str]
    project_retry_count: int
    zero_byte_assets: list[str]


def gather_facts(project_id: str) -> ProjectFacts:
    storage = get_storage_backend()
    with sync_session_scope() as session:
        project = session.get(Project, UUID(project_id))
        if project is None:
            raise LookupError(f"project {project_id} not found")

        script = session.scalar(
            select(Script).where(Script.project_id == UUID(project_id)).order_by(Script.version.desc())
        )
        segments = list(script.segments) if script else []
        narration_total = 0.0
        for segment in segments:
            voiceover = session.scalar(
                select(Voiceover).where(Voiceover.script_segment_id == segment.id)
            )
            if voiceover and voiceover.duration_sec is not None:
                narration_total += float(voiceover.duration_sec)

        images = list(
            session.scalars(
                select(Asset).where(
                    Asset.project_id == UUID(project_id),
                    Asset.type == PhysicalAssetType.IMAGE,
                )
            )
        )
        # The thumbnail is a real image asset but is not part of the
        # video's visual sequence, so it must not count toward reuse.
        video_images = [a for a in images if "/thumbnail/" not in a.storage_path]
        image_paths = [a.storage_path for a in video_images]

        render = session.scalar(select(Render).where(Render.project_id == UUID(project_id)))
        render_asset = session.get(Asset, render.asset_id) if render else None

        all_assets = list(
            session.scalars(select(Asset).where(Asset.project_id == UUID(project_id)))
        )
        stub_assets = [
            a.storage_path for a in all_assets if (a.provider or "").lower().startswith("stub")
        ]
        zero_byte = []
        for asset in all_assets:
            try:
                if len(storage.read_bytes(asset.storage_path)) == 0:
                    zero_byte.append(asset.storage_path)
            except Exception:  # noqa: BLE001 - unreadable is reported by its own check
                zero_byte.append(f"{asset.storage_path} (unreadable)")

        jobs = list(session.scalars(select(Job).where(Job.project_id == UUID(project_id))))
        retried = [f"{j.agent_name} x{j.attempt_count}" for j in jobs if j.attempt_count > 1]

        return ProjectFacts(
            narration_total_sec=narration_total,
            segment_count=len(segments),
            distinct_narration_texts=len({s.text.strip() for s in segments}),
            image_asset_rows=len(video_images),
            distinct_image_paths=len(set(image_paths)),
            image_paths=image_paths,
            render_storage_path=render_asset.storage_path if render_asset else None,
            render_duration_sec=float(render.duration_sec) if render and render.duration_sec else None,
            stub_provider_assets=stub_assets,
            jobs_with_retries=retried,
            project_retry_count=project.retry_count,
            zero_byte_assets=zero_byte,
        )


# --- the checks -------------------------------------------------------------


def run_checks(project_id: str, video: Path, facts: ProjectFacts, args) -> Report:
    report = Report(project_id=project_id)
    probe = _ffprobe_json(video)
    video_stream = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    audio_stream = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
    container_duration = float(probe["format"]["duration"])

    report.metrics["video_duration_sec"] = round(container_duration, 3)
    report.metrics["narration_duration_sec"] = round(facts.narration_total_sec, 3)
    report.metrics["resolution"] = (
        f"{video_stream['width']}x{video_stream['height']}" if video_stream else "unknown"
    )
    report.metrics["script_segments"] = facts.segment_count
    report.metrics["images_generated"] = facts.image_asset_rows
    report.metrics["unique_images"] = facts.distinct_image_paths

    # 1. duration vs narration
    drift = abs(container_duration - facts.narration_total_sec)
    report.add(
        1, "Video duration matches narration",
        drift <= args.duration_tolerance,
        f"video {container_duration:.2f}s vs narration {facts.narration_total_sec:.2f}s "
        f"(drift {drift:.2f}s, tolerance {args.duration_tolerance:.2f}s)",
    )

    with tempfile.TemporaryDirectory(prefix="acceptance_") as tmp:
        work_dir = Path(tmp)
        frames = _sample_frames(video, args.sample_interval, work_dir)
        if len(frames) < 3:
            report.add(2, "Images change throughout", False, "too few frames decoded to judge")
            return report

        grays = [_load_gray(path) for path in frames]
        diffs = [_mean_abs_diff(grays[i - 1], grays[i]) for i in range(1, len(grays))]
        changes = [d for d in diffs if d >= args.frame_change_threshold]

        # 2. images change throughout — and not just in one burst
        expected_changes = max(int(container_duration / args.max_avg_image_sec) - 1, 1)
        report.metrics["visual_changes_detected"] = len(changes)
        report.add(
            2, "Images change throughout",
            len(changes) >= expected_changes,
            f"{len(changes)} visual changes detected across {container_duration:.1f}s "
            f"(expected at least {expected_changes})",
        )

        # 3/5. repeated or looping footage, measured on pixels
        frame_hashes = [_perceptual_hash(g) for g in grays]
        repeats = [h for h, n in Counter(frame_hashes).items() if n > 1]
        # A held frame legitimately repeats within its own shot; a repeat
        # separated by other content is footage coming back around.
        looping = _has_non_adjacent_repeat(frame_hashes)
        report.add(
            3, "No repeated images (file level)",
            facts.distinct_image_paths == facts.image_asset_rows,
            f"{facts.image_asset_rows} image assets, {facts.distinct_image_paths} unique files",
        )
        report.add(
            5, "No visual looping (pixel level)",
            not looping,
            "no frame content reappears after other content"
            if not looping
            else f"{len(repeats)} frame(s) reappear later in the video — footage is looping",
        )

        # 6. pacing
        shot_count = len(changes) + 1
        avg_image_sec = container_duration / shot_count if shot_count else container_duration
        report.metrics["avg_image_duration_sec"] = round(avg_image_sec, 2)
        report.add(
            6, "Pacing resembles a documentary, not a slideshow",
            args.min_avg_image_sec <= avg_image_sec <= args.max_avg_image_sec,
            f"average {avg_image_sec:.2f}s on screen per visual "
            f"(target {args.min_avg_image_sec}-{args.max_avg_image_sec}s)",
        )

        # 7. sharpness
        sharpness = [_laplacian_variance(g) for g in grays]
        median_sharpness = sorted(sharpness)[len(sharpness) // 2]
        report.metrics["median_sharpness"] = round(median_sharpness, 2)
        report.add(
            7, "Images are sharp",
            median_sharpness >= args.sharpness_threshold,
            f"median Laplacian variance {median_sharpness:.1f} "
            f"(threshold {args.sharpness_threshold})",
        )

        # 8. camera movement within a shot
        small_moves = [d for d in diffs if args.motion_threshold <= d < args.frame_change_threshold]
        report.metrics["intra_shot_motion_samples"] = len(small_moves)
        report.add(
            8, "Camera movement applied",
            len(small_moves) > 0,
            f"{len(small_moves)} sample pairs show motion within a held shot "
            "(Ken Burns push/pan)",
        )

        # 9. transitions blend rather than hard-cut
        blended = _count_gradual_transitions(diffs, args.frame_change_threshold)
        report.add(
            9, "Transitions are smooth",
            blended > 0 or len(changes) == 0,
            f"{blended} of {len(changes)} visual changes ramp across samples "
            "rather than switching in one frame",
        )

        # 10. not truncated — the final frame must be real content
        last_sharpness = sharpness[-1]
        report.add(
            10, "Video ends naturally",
            last_sharpness >= args.sharpness_threshold,
            f"final frame Laplacian variance {last_sharpness:.1f} "
            "(a black or empty tail would be near zero)",
        )

    # 11. audio/video stream alignment
    if audio_stream is None:
        report.add(11, "Audio stays synchronized", False, "no audio stream in the output")
    else:
        audio_duration = float(audio_stream.get("duration") or container_duration)
        video_duration = float(video_stream.get("duration") or container_duration)
        av_drift = abs(audio_duration - video_duration)
        report.metrics["audio_video_drift_sec"] = round(av_drift, 3)
        report.add(
            11, "Audio stays synchronized",
            av_drift <= args.duration_tolerance,
            f"audio {audio_duration:.2f}s vs video {video_duration:.2f}s (drift {av_drift:.2f}s)",
        )

    # 4. repeated narration
    report.add(
        4, "No repeated narration",
        facts.distinct_narration_texts == facts.segment_count,
        f"{facts.segment_count} segments, {facts.distinct_narration_texts} distinct texts",
    )

    # 12/13/14/15 — from the pipeline's own durable record
    report.add(
        12, "No FFmpeg errors",
        facts.render_storage_path is not None,
        "render completed and persisted a Render row"
        if facts.render_storage_path
        else "no Render row — the compositor did not produce output",
    )
    report.add(
        13, "No provider silently failed",
        not facts.stub_provider_assets,
        "no asset was produced by a Stub* provider"
        if not facts.stub_provider_assets
        else f"{len(facts.stub_provider_assets)} asset(s) came from a stub provider",
    )
    report.add(
        14, "No retry loops",
        not facts.jobs_with_retries and facts.project_retry_count == 0,
        f"every job succeeded first attempt; project retry_count={facts.project_retry_count}"
        if not facts.jobs_with_retries
        else f"retried jobs: {', '.join(facts.jobs_with_retries)}",
    )
    report.add(
        15, "No placeholder assets",
        not facts.zero_byte_assets,
        "every persisted asset has real bytes"
        if not facts.zero_byte_assets
        else f"{len(facts.zero_byte_assets)} empty/unreadable asset(s)",
    )
    return report


def _perceptual_hash(gray) -> str:
    """A coarse 8x8 average hash — robust to codec noise, sensitive to
    actual content change. Used to spot the same shot reappearing later.
    """
    import numpy as np

    height, width = gray.shape
    block_h, block_w = max(height // 8, 1), max(width // 8, 1)
    blocks = gray[: block_h * 8, : block_w * 8].reshape(8, block_h, 8, block_w).mean(axis=(1, 3))
    return "".join("1" if value > blocks.mean() else "0" for value in blocks.flatten())


def _has_non_adjacent_repeat(hashes: list[str]) -> bool:
    """True when a frame's content reappears after *different* content in
    between — the signature of looping. Consecutive repeats are just a
    visual being held, which is expected.
    """
    first_seen: dict[str, int] = {}
    for index, value in enumerate(hashes):
        if value in first_seen and index - first_seen[value] > 1:
            if any(other != value for other in hashes[first_seen[value] + 1 : index]):
                return True
        first_seen.setdefault(value, index)
    return False


def _count_gradual_transitions(diffs: list[float], change_threshold: float) -> int:
    """How many visual changes are spread over more than one sample — a
    dissolve raises the frame-to-frame difference across consecutive
    samples, where a hard cut spikes exactly once.
    """
    count = 0
    for index, value in enumerate(diffs):
        if value < change_threshold:
            continue
        neighbours = [
            diffs[i] for i in (index - 1, index + 1) if 0 <= i < len(diffs)
        ]
        if any(n >= change_threshold * 0.25 for n in neighbours):
            count += 1
    return count


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--video", default=None, help="Override the MP4 path (defaults to the project's Render row).")
    parser.add_argument("--duration-tolerance", type=float, default=DEFAULT_DURATION_TOLERANCE_SEC)
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SEC)
    parser.add_argument("--frame-change-threshold", type=float, default=DEFAULT_FRAME_CHANGE_THRESHOLD)
    parser.add_argument("--motion-threshold", type=float, default=DEFAULT_MOTION_THRESHOLD)
    parser.add_argument("--sharpness-threshold", type=float, default=DEFAULT_SHARPNESS_THRESHOLD)
    parser.add_argument("--min-avg-image-sec", type=float, default=DEFAULT_MIN_AVG_IMAGE_SEC)
    parser.add_argument("--max-avg-image-sec", type=float, default=DEFAULT_MAX_AVG_IMAGE_SEC)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        print("ffmpeg/ffprobe not on PATH", file=sys.stderr)
        return 2

    facts = gather_facts(args.project_id)
    if args.video:
        video = Path(args.video)
    elif facts.render_storage_path:
        storage = get_storage_backend()
        video = Path(tempfile.mkdtemp(prefix="acceptance_video_")) / "render.mp4"
        video.write_bytes(storage.read_bytes(facts.render_storage_path))
    else:
        print(f"project {args.project_id} has no render to check", file=sys.stderr)
        return 2

    report = run_checks(args.project_id, video, facts, args)

    print(f"\n=== Acceptance report for {report.project_id} ===\n")
    for result in sorted(report.results, key=lambda r: r.number):
        mark = "SKIP" if result.skipped else ("PASS" if result.passed else "FAIL")
        print(f"  [{mark}] {result.number:2d}. {result.name}")
        print(f"          {result.detail}")
    print("\n--- metrics ---")
    for key, value in report.metrics.items():
        print(f"  {key}: {value}")

    if report.failed:
        print(f"\nFAILED {len(report.failed)} of {len(report.results)} checks")
        return 1
    print(f"\nAll {len(report.results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
