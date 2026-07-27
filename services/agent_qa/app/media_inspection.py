"""Shared ffprobe/ffmpeg technical-inspection helpers for the Quality
Control Agent's `VideoReviewer`/`AudioReviewer` (reviewers/) — both
analyze the same rendered file (different commands, different findings),
so the subprocess plumbing lives in one place instead of being
duplicated across two reviewer classes.

Every function here raises `MediaInspectionError` if the inspection
*command itself* fails to run (a missing/corrupt file, ffmpeg not
installed) — that is never a QA *finding*; a reviewer turns a genuine
finding into an `Issue`, and lets a broken inspection propagate as a
real job failure instead of silently reporting "no problems found".
"""

import json
import re
import subprocess
from dataclasses import dataclass

#: A full decode (silencedetect/astats/blackdetect/decode_check) budget
#: per inspection call — generous for the short clips this pipeline
#: produces today; revisit if real production-length renders need more.
_INSPECTION_TIMEOUT_SEC = 120


class MediaInspectionError(RuntimeError):
    """An ffprobe/ffmpeg inspection command itself failed to run."""


@dataclass(frozen=True)
class StreamInfo:
    codec_type: str
    codec_name: str
    width: int | None
    height: int | None
    duration_sec: float | None


@dataclass(frozen=True)
class ProbeResult:
    duration_sec: float
    streams: list[StreamInfo]

    def video_stream(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    def audio_stream(self) -> StreamInfo | None:
        return next((s for s in self.streams if s.codec_type == "audio"), None)


def probe_streams(path: str) -> ProbeResult:
    """Metadata-only inspection (no decode) — resolution, codec, and
    per-stream/overall duration.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_INSPECTION_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise MediaInspectionError(
            f"ffprobe failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
        )
    data = json.loads(result.stdout)
    streams = [
        StreamInfo(
            codec_type=stream.get("codec_type", ""),
            codec_name=stream.get("codec_name", ""),
            width=stream.get("width"),
            height=stream.get("height"),
            duration_sec=float(stream["duration"]) if stream.get("duration") else None,
        )
        for stream in data.get("streams", [])
    ]
    duration_sec = float(data.get("format", {}).get("duration") or 0.0)
    return ProbeResult(duration_sec=duration_sec, streams=streams)


def decode_check(path: str) -> str | None:
    """Attempts a full decode of every stream in `path`. Returns `None`
    if it decoded cleanly, else the tail of ffmpeg's own error output —
    a real corrupt-file/rendering-problem signal, not a guess.
    """
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_INSPECTION_TIMEOUT_SEC,
    )
    stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or stderr_text:
        return stderr_text[-500:] if stderr_text else f"ffmpeg exited {result.returncode}"
    return None


def detect_silence(
    path: str, *, noise_floor_db: float = -35.0, min_silence_sec: float = 1.5
) -> list[tuple[float, float]]:
    """Every silence gap at least `min_silence_sec` long, quieter than
    `noise_floor_db`, via ffmpeg's `silencedetect` filter. Needs
    `-loglevel info` (not the usual `-v error`) — the filter reports
    `silence_start`/`silence_end` as INFO-level log lines on stderr, not
    as a distinct output stream.
    """
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "info", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={noise_floor_db}dB:d={min_silence_sec}",
         "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_INSPECTION_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise MediaInspectionError(
            f"silencedetect failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
        )
    return _parse_interval_events(
        result.stderr.decode("utf-8", errors="replace"), start_marker="silence_start", end_marker="silence_end"
    )


def detect_black_frames(
    path: str, *, black_min_duration_sec: float = 0.5, pic_threshold: float = 0.98
) -> list[tuple[float, float]]:
    """Every black-frame span at least `black_min_duration_sec` long via
    ffmpeg's `blackdetect` filter — same INFO-level-logging caveat as
    `detect_silence`.
    """
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "info", "-nostats", "-i", path,
         "-vf", f"blackdetect=d={black_min_duration_sec}:pic_th={pic_threshold}",
         "-an", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_INSPECTION_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise MediaInspectionError(
            f"blackdetect failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
        )
    return _parse_interval_events(
        result.stderr.decode("utf-8", errors="replace"), start_marker="black_start", end_marker="black_end"
    )


def measure_peak_level_db(path: str) -> float | None:
    """The audio track's overall peak level in dBFS via ffmpeg's `astats`
    filter, or `None` if it couldn't be parsed (e.g. no audio stream). A
    peak at/near 0 dBFS is a real clipping signal — full-scale samples
    don't happen in cleanly-mixed audio. Needs default (INFO) logging,
    same reason as `detect_silence`.

    `astats` prints a per-channel block first, then one whole-track
    "Overall" block (each `[Parsed_astats_N @ 0x...] `-prefixed) — only
    the value(s) in the "Overall" block reflect the entire track, so
    earlier per-channel figures are deliberately ignored (the last
    matching line in that block wins, in case of a multi-channel report).
    """
    result = subprocess.run(
        ["ffmpeg", "-nostats", "-i", path, "-af", "astats=metadata=0", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_INSPECTION_TIMEOUT_SEC,
    )
    if result.returncode != 0:
        raise MediaInspectionError(
            f"astats failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
        )
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    _, _, overall_section = stderr_text.rpartition("] Overall")
    peak_db: float | None = None
    for line in overall_section.splitlines():
        if "Peak level dB:" in line:
            try:
                peak_db = float(line.rsplit("Peak level dB:", 1)[1].strip())
            except ValueError:
                continue
    return peak_db


def _parse_interval_events(stderr_text: str, *, start_marker: str, end_marker: str) -> list[tuple[float, float]]:
    """Handles both observed ffmpeg filter log shapes: `silencedetect`
    puts `{marker}_start`/`{marker}_end` on separate lines
    (`silence_start: 1.999` ... `silence_end: 4.000 | silence_duration: ...`),
    while `blackdetect` puts both on the *same* line
    (`black_start:2 black_end:3 black_duration:1`, no space after the
    colon) — checking both regexes unconditionally on every line handles
    either shape without needing to know which one a given filter uses.
    """
    start_re = re.compile(rf"{start_marker}:\s*([\d.]+)")
    end_re = re.compile(rf"{end_marker}:\s*([\d.]+)")
    intervals: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in stderr_text.splitlines():
        start_match = start_re.search(line)
        if start_match:
            pending_start = float(start_match.group(1))
        end_match = end_re.search(line)
        if end_match and pending_start is not None:
            intervals.append((pending_start, float(end_match.group(1))))
            pending_start = None
    return intervals
