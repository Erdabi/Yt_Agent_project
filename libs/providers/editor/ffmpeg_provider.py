"""A real, local ffmpeg-based compositor.

Unlike every other provider in `libs/providers`, this one needs no
vendor account or API key — `ffmpeg`/`ffprobe` are free binaries this
process shells out to (see services/agent_video/Dockerfile). Given an
`EditSpec`, it: normalizes every visual clip (image or video) to the
target resolution/frame rate and the exact duration its segment needs,
mixes each segment's narration with any supplementary audio, applies a
plain fade for any non-"cut" transition between segments (a real but
deliberately simple treatment — distinct wipe/slide/zoom/dissolve
filtergraphs per `TransitionType` value are a documented future
enhancement, not implemented here), concatenates every segment (plus an
optional intro/outro) into one video, and burns in every subtitle cue in
a single final pass over the fully assembled timeline.

All work happens in bytes-in/bytes-out fashion, in a per-call temporary
directory that's always cleaned up — this provider knows nothing about
`libs.storage`; Rendering (services/agent_video/app/modules/rendering.py)
is the only thing that reads/writes storage, the same separation every
other provider already keeps.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import EditorProvider, EditResult, EditSegment, EditSpec, SubtitleLine


class FFmpegEditorProvider(EditorProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        self._ffmpeg = config.get("ffmpeg_binary", "ffmpeg")
        self._ffprobe = config.get("ffprobe_binary", "ffprobe")
        self._fps = int(config.get("fps", 30))
        self._font_path = config.get(
            "font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        )
        self._crossfade_sec = float(config.get("crossfade_sec", 0.5))
        self._audio_bitrate = config.get("audio_bitrate", "128k")

    def render(self, spec: EditSpec) -> EditResult:
        width, height = self._parse_resolution(spec.resolution)
        work_dir = tempfile.mkdtemp(prefix="ffmpeg_editor_")
        try:
            segment_count = len(spec.segments)
            segment_files = []
            for index, segment in enumerate(spec.segments):
                fade_in = index > 0 and spec.segments[index - 1].transition_type != "cut"
                fade_out = index < segment_count - 1 and segment.transition_type != "cut"
                segment_files.append(
                    self._build_segment(work_dir, index, segment, width, height, fade_in, fade_out)
                )

            intro_path = (
                self._normalize_branding(work_dir, "intro", spec.intro_asset, width, height)
                if spec.intro_asset
                else None
            )
            outro_path = (
                self._normalize_branding(work_dir, "outro", spec.outro_asset, width, height)
                if spec.outro_asset
                else None
            )
            ordered = [*([intro_path] if intro_path else []), *segment_files, *([outro_path] if outro_path else [])]
            assembled = self._concat(work_dir, "assembled", ordered)

            final_path = self._burn_subtitles(work_dir, assembled, spec.subtitle_lines, width, height)

            video_bytes = Path(final_path).read_bytes()
            duration = self._probe_duration(final_path)
            return EditResult(video_bytes=video_bytes, duration_sec=duration, resolution=spec.resolution)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _build_segment(
        self,
        work_dir: str,
        index: int,
        segment: EditSegment,
        width: int,
        height: int,
        fade_in: bool,
        fade_out: bool,
    ) -> str:
        duration = max(segment.duration_sec, 0.1)
        prefix = f"seg{index}"

        if segment.visual_clips:
            clip_count = len(segment.visual_clips)
            per_clip_duration = duration / clip_count
            clip_paths = [
                self._normalize_visual(
                    work_dir, f"{prefix}_v{i}", clip.data, clip.kind, per_clip_duration, width, height
                )
                for i, clip in enumerate(segment.visual_clips)
            ]
        else:
            clip_paths = [self._black_clip(work_dir, f"{prefix}_v0", duration, width, height)]
        visual_raw = self._concat(work_dir, f"{prefix}_visual", clip_paths)

        audio_paths = [
            self._normalize_audio(work_dir, f"{prefix}_a{i}", data, duration)
            for i, data in enumerate([segment.voice_audio, *segment.supplementary_audio])
        ]
        audio_mixed = self._mix_audio(work_dir, f"{prefix}_audio", audio_paths)

        fade_dur = min(self._crossfade_sec, duration / 2)
        vf_parts = [
            self._drawtext_filter(text, y_offset=i * 70) for i, text in enumerate(segment.overlay_texts)
        ]
        if fade_in:
            vf_parts.append(f"fade=t=in:st=0:d={fade_dur:.3f}")
        if fade_out:
            vf_parts.append(f"fade=t=out:st={duration - fade_dur:.3f}:d={fade_dur:.3f}")
        vf_chain = ",".join(vf_parts) if vf_parts else "null"

        af_parts = []
        if fade_in:
            af_parts.append(f"afade=t=in:st=0:d={fade_dur:.3f}")
        if fade_out:
            af_parts.append(f"afade=t=out:st={duration - fade_dur:.3f}:d={fade_dur:.3f}")
        af_chain = ",".join(af_parts) if af_parts else "anull"

        out = os.path.join(work_dir, f"{prefix}_final.mp4")
        filter_complex = f"[0:v]{vf_chain}[v];[1:a]{af_chain}[a]"
        self._run([
            self._ffmpeg, "-y", "-i", visual_raw, "-i", audio_mixed,
            "-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
            "-r", str(self._fps), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", self._audio_bitrate, "-shortest", out,
        ])
        return out

    def _normalize_branding(self, work_dir: str, name: str, data: bytes, width: int, height: int) -> str:
        src = self._write_temp(work_dir, f"{name}_src.mp4", data)
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run([
            self._ffmpeg, "-y", "-i", src, "-vf", self._scale_pad_filter(width, height),
            "-r", str(self._fps), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", self._audio_bitrate, out,
        ])
        return out

    def _normalize_visual(
        self, work_dir: str, name: str, data: bytes, kind: str, duration: float, width: int, height: int
    ) -> str:
        extension = "png" if kind == "image" else "mp4"
        src = self._write_temp(work_dir, f"{name}_src.{extension}", data)
        out = os.path.join(work_dir, f"{name}.mp4")
        vf = self._scale_pad_filter(width, height)
        loop_args = ["-loop", "1"] if kind == "image" else ["-stream_loop", "-1"]
        self._run([
            self._ffmpeg, "-y", *loop_args, "-i", src, "-t", f"{duration:.3f}",
            "-vf", vf, "-pix_fmt", "yuv420p", "-an", out,
        ])
        return out

    def _black_clip(self, work_dir: str, name: str, duration: float, width: int, height: int) -> str:
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run([
            self._ffmpeg, "-y", "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d={duration:.3f}:r={self._fps}",
            "-pix_fmt", "yuv420p", out,
        ])
        return out

    def _normalize_audio(self, work_dir: str, name: str, data: bytes, duration: float) -> str:
        src = self._write_temp(work_dir, f"{name}_src.mp3", data)
        out = os.path.join(work_dir, f"{name}.wav")
        self._run([
            self._ffmpeg, "-y", "-i", src, "-af", "apad", "-t", f"{duration:.3f}",
            "-ar", "44100", "-ac", "2", out,
        ])
        return out

    def _mix_audio(self, work_dir: str, name: str, audio_paths: list[str]) -> str:
        if len(audio_paths) == 1:
            return audio_paths[0]
        out = os.path.join(work_dir, f"{name}.wav")
        cmd = [self._ffmpeg, "-y"]
        for path in audio_paths:
            cmd += ["-i", path]
        cmd += [
            "-filter_complex", f"amix=inputs={len(audio_paths)}:duration=first:dropout_transition=0",
            "-ar", "44100", "-ac", "2", out,
        ]
        self._run(cmd)
        return out

    def _concat(self, work_dir: str, name: str, paths: list[str]) -> str:
        if len(paths) == 1:
            return paths[0]
        list_path = os.path.join(work_dir, f"{name}_list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for path in paths:
                f.write(f"file '{path}'\n")
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run([self._ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out])
        return out

    def _burn_subtitles(
        self, work_dir: str, assembled_path: str, subtitle_lines: list[SubtitleLine], width: int, height: int
    ) -> str:
        if not subtitle_lines:
            return assembled_path
        parts = []
        for cue in subtitle_lines:
            base = self._drawtext_filter(cue.text, y_offset=0, emphasized=cue.emphasized)
            parts.append(f"{base}:enable='between(t,{cue.start_sec:.3f},{cue.end_sec:.3f})'")
        out = os.path.join(work_dir, "final.mp4")
        self._run([
            self._ffmpeg, "-y", "-i", assembled_path, "-vf", ",".join(parts),
            "-r", str(self._fps), "-pix_fmt", "yuv420p", "-c:a", "copy", out,
        ])
        return out

    def _drawtext_filter(self, text: str, *, y_offset: int = 0, emphasized: bool = False) -> str:
        color = "yellow" if emphasized else "white"
        escaped_text = self._escape_for_filter(text)
        escaped_font = self._escape_for_filter(self._font_path)
        return (
            f"drawtext=fontfile={escaped_font}:text='{escaped_text}':fontsize=44:"
            f"fontcolor={color}:borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=h-160-{y_offset}"
        )

    def _scale_pad_filter(self, width: int, height: int) -> str:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self._fps}"
        )

    def _probe_duration(self, path: str) -> float:
        result = subprocess.run(
            [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
            )
        return float(result.stdout.decode("utf-8").strip())

    def _run(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg command failed (exit {result.returncode}): {' '.join(cmd)}\n"
                f"{result.stderr.decode('utf-8', errors='replace')[-4000:]}"
            )

    @staticmethod
    def _write_temp(work_dir: str, name: str, data: bytes) -> str:
        path = os.path.join(work_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    @staticmethod
    def _parse_resolution(resolution: str) -> tuple[int, int]:
        width_str, _, height_str = resolution.partition("x")
        return int(width_str), int(height_str)

    @staticmethod
    def _escape_for_filter(value: str) -> str:
        value = value.replace("\\", "\\\\")
        value = value.replace(":", "\\:")
        value = value.replace("'", "\\'")
        value = value.replace("%", "\\%")
        return value
