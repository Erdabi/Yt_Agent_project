"""A real, local ffmpeg-based compositor.

Unlike every other provider in `libs/providers`, this one needs no
vendor account or API key — `ffmpeg`/`ffprobe` are free binaries this
process shells out to (see services/agent_video/Dockerfile). Given an
`EditSpec`, it: normalizes every visual clip (image or video) to the
resolved `RenderProfile`'s resolution/frame rate and the exact duration
its segment needs, mixes each segment's narration with any supplementary
audio, applies a plain fade for any non-"cut" transition between
segments (a real but deliberately simple treatment — distinct wipe/
slide/zoom/dissolve filtergraphs per `TransitionType` value are a
documented future enhancement, not implemented here), concatenates every
segment (plus an optional intro/outro), mixes in an optional whole-video
background-music bed (looped, ducked under the existing mix), normalizes
the final audio to the profile's loudness target (single-pass EBU R128
`loudnorm` — real and working, though less precise than a two-pass
analyze-then-normalize approach), and burns in every subtitle cue in
that same final pass.

All work happens in bytes-in/bytes-out fashion, in a per-call temporary
directory that's always cleaned up — this provider knows nothing about
`libs.storage`; Rendering (services/agent_video/app/modules/rendering.py)
is the only thing that reads/writes storage, the same separation every
other provider already keeps. Progress is reported through an optional
callback (`RenderProgress`, one event per pipeline stage) rather than
this provider knowing anything about how a caller displays or persists
it; every failure is one of `EditorInputError`/`EditorTimeoutError`/
`EditorRenderError` (base.py) instead of a bare exception, so a caller
can distinguish "fix your input and retry" from "the compositor itself
broke" without parsing free-text messages.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import (
    EditorError,
    EditorInputError,
    EditorProvider,
    EditorRenderError,
    EditorTimeoutError,
    EditResult,
    EditSegment,
    EditSpec,
    ProgressCallback,
    RenderProgress,
    SubtitleLine,
)
from .profiles import RenderProfile, RenderProfileError, get_render_profile

#: Fixed step count for the stages surrounding the per-segment loop:
#: assemble, background-music mix, finalize (loudnorm + subtitles),
#: probe the final output.
_FIXED_STAGE_COUNT = 4


class FFmpegEditorProvider(EditorProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        self._ffmpeg = config.get("ffmpeg_binary", "ffmpeg")
        self._ffprobe = config.get("ffprobe_binary", "ffprobe")
        self._font_path = config.get(
            "font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        )
        self._audio_bitrate = config.get("audio_bitrate", "128k")
        #: Per-subprocess-call budget — bounds a single stuck ffmpeg/
        #: ffprobe invocation rather than the render as a whole, since
        #: individual steps (normalizing one small clip vs. concatenating
        #: the full video) naturally take very different amounts of time.
        self._ffmpeg_timeout_sec = float(config.get("ffmpeg_timeout_sec", 300))
        #: Linear gain applied to a whole-video background-music bed so
        #: it sits under narration/supplementary audio rather than
        #: competing with it — a plain volume multiplier, not loudness-
        #: aware ducking keyed to when narration is actually speaking.
        self._background_music_volume = float(config.get("background_music_volume", 0.18))

    def render(self, spec: EditSpec, *, on_progress: ProgressCallback | None = None) -> EditResult:
        profile = self._resolve_profile(spec.profile_name)
        self._validate_spec(spec)
        width, height = self._parse_resolution(profile.resolution)
        segment_count = len(spec.segments)
        total_steps = segment_count + _FIXED_STAGE_COUNT

        work_dir = tempfile.mkdtemp(prefix="ffmpeg_editor_")
        try:
            segment_files = []
            for index, segment in enumerate(spec.segments):
                self._report(
                    on_progress, "normalize_segments", index + 1, total_steps,
                    f"segment {index + 1}/{segment_count} ({segment.segment_id})",
                )
                fade_in = index > 0 and spec.segments[index - 1].transition_type != "cut"
                fade_out = index < segment_count - 1 and segment.transition_type != "cut"
                segment_files.append(
                    self._build_segment(work_dir, index, segment, width, height, profile, fade_in, fade_out)
                )

            self._report(on_progress, "assemble", segment_count + 1, total_steps, "concatenating segments")
            intro_path = (
                self._normalize_branding(work_dir, "intro", spec.intro_asset, width, height, profile)
                if spec.intro_asset else None
            )
            outro_path = (
                self._normalize_branding(work_dir, "outro", spec.outro_asset, width, height, profile)
                if spec.outro_asset else None
            )
            ordered = [*([intro_path] if intro_path else []), *segment_files, *([outro_path] if outro_path else [])]
            assembled = self._concat(work_dir, "assembled", ordered)

            self._report(
                on_progress, "mix_background_music", segment_count + 2, total_steps,
                "mixing background music" if spec.background_music else "no background music configured",
            )
            if spec.background_music:
                assembled = self._mix_background_music(
                    work_dir, assembled, spec.background_music, spec.total_duration_sec
                )

            self._report(
                on_progress, "finalize", segment_count + 3, total_steps,
                "normalizing loudness and burning in subtitles",
            )
            final_path = self._finalize(work_dir, assembled, spec.subtitle_lines, profile)

            self._report(on_progress, "probe_output", segment_count + 4, total_steps, "reading final output")
            video_bytes = Path(final_path).read_bytes()
            duration = self._probe_duration(final_path)
            return EditResult(video_bytes=video_bytes, duration_sec=duration, resolution=profile.resolution)
        except EditorError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately broad: every failure must surface as an EditorError
            raise EditorRenderError(f"unexpected compositor failure: {exc}") from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # --- input validation -------------------------------------------------

    @staticmethod
    def _resolve_profile(profile_name: str) -> RenderProfile:
        try:
            return get_render_profile(profile_name)
        except RenderProfileError as exc:
            raise EditorInputError(str(exc)) from exc

    @staticmethod
    def _validate_spec(spec: EditSpec) -> None:
        if not spec.segments:
            raise EditorInputError(f"project {spec.project_id}: EditSpec has no segments to render")
        for segment in spec.segments:
            if not segment.voice_audio:
                raise EditorInputError(
                    f"project {spec.project_id}, segment {segment.segment_id!r}: no voice_audio provided"
                )

    @staticmethod
    def _report(
        on_progress: ProgressCallback | None, stage: str, current: int, total: int, message: str = ""
    ) -> None:
        if on_progress is None:
            return
        on_progress(RenderProgress(stage=stage, current_step=current, total_steps=total, message=message))

    # --- per-segment compositing -------------------------------------------

    def _build_segment(
        self,
        work_dir: str,
        index: int,
        segment: EditSegment,
        width: int,
        height: int,
        profile: RenderProfile,
        fade_in: bool,
        fade_out: bool,
    ) -> str:
        duration = max(segment.duration_sec, 0.1)
        prefix = f"seg{index}"

        if segment.visual_clips:
            clip_durations = self._clip_durations(segment, duration)
            # A dissolve consumes overlap — joining N clips with xfade
            # yields a track (N-1) x fade shorter than their sum. Render
            # each clip (except the last) that much longer up front so
            # the joined track lands back on exactly `duration`, and
            # every visual still starts when its beat says it should.
            # Compensating here rather than stretching the result keeps
            # visuals aligned to the narration they were planned against.
            fade = self._visual_fade(profile, clip_durations)
            padded_durations = [
                d + fade if index < len(clip_durations) - 1 else d
                for index, d in enumerate(clip_durations)
            ]
            clip_paths = [
                self._normalize_visual(
                    work_dir,
                    f"{prefix}_v{i}",
                    clip.data,
                    clip.kind,
                    padded_durations[i],
                    width,
                    height,
                    profile,
                    # Alternating direction per clip so consecutive
                    # visuals don't all drift the same way, which reads
                    # as a mechanical effect rather than as camera move.
                    zoom_in=(i % 2 == 0),
                )
                for i, clip in enumerate(segment.visual_clips)
            ]
        else:
            clip_paths = [self._black_clip(work_dir, f"{prefix}_v0", duration, width, height, profile)]
        visual_raw = self._concat_visuals(work_dir, f"{prefix}_visual", clip_paths, profile)

        audio_paths = [
            self._normalize_audio(work_dir, f"{prefix}_a{i}", data, duration, f"segment {segment.segment_id} audio {i}")
            for i, data in enumerate([segment.voice_audio, *segment.supplementary_audio])
        ]
        audio_mixed = self._mix_audio(work_dir, f"{prefix}_audio", audio_paths)

        fade_dur = min(profile.crossfade_sec, duration / 2)
        vf_parts = [
            # Safety net, first in the chain: hold the final frame if the
            # visual track comes up short. Narration is the authority on
            # how long a segment lasts, so a visual track even marginally
            # shorter than the audio (rounding on any per-clip duration,
            # a crossfade cap that bit deeper than expected) would
            # otherwise end the segment early and truncate real
            # narration. `stop_duration` rather than an unbounded
            # `stop=-1`: an infinite filter output never lets ffmpeg
            # decide it is done, and the whole command hangs until its
            # timeout instead of producing a segment. Bounded here, and
            # bounded again by the explicit `-t` below.
            f"tpad=stop_mode=clone:stop_duration={duration:.3f}",
            *(
                self._drawtext_filter(
                    work_dir, f"{prefix}_overlay{i}", text, profile.subtitle_font_size, y_offset=i * 70
                )
                for i, text in enumerate(segment.overlay_texts)
            ),
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
        self._run(
            [
                self._ffmpeg, "-y", "-i", visual_raw, "-i", audio_mixed,
                "-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
                "-r", str(profile.fps), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", self._audio_bitrate,
                # `-t` rather than `-shortest`: the segment's length is a
                # known quantity (its narration), not something to infer
                # from whichever track happens to end first. Stating it
                # explicitly is also what makes the padded video stream
                # above terminate.
                "-t", f"{duration:.3f}", out,
            ],
            stage=f"composite segment {segment.segment_id}",
        )
        return out

    @staticmethod
    def _visual_fade(profile: RenderProfile, clip_durations: list[float]) -> float:
        """The dissolve length actually usable between this segment's
        visuals: the profile's setting, capped so it can never exceed
        half the shortest clip it has to blend (beyond that ffmpeg's
        `xfade` produces a black gap rather than a blend). `0` for a
        single visual, which has nothing to dissolve into.
        """
        if len(clip_durations) < 2 or profile.visual_crossfade_sec <= 0:
            return 0.0
        return max(min(profile.visual_crossfade_sec, min(clip_durations) / 2), 0.0)

    @staticmethod
    def _clip_durations(segment: EditSegment, duration: float) -> list[float]:
        """How long each of a segment's visuals holds the screen.

        Prefers the per-clip durations the caller computed from its own
        visual beats (`VisualClip.duration_sec`), rescaled so they add up
        to exactly the segment's narration-driven duration — a beat plan
        is built from the same narration, so the two agree to within
        rounding, but rescaling makes "visuals exactly fill the segment"
        a property of this code rather than an assumption about the
        caller's arithmetic.

        Falls back to an even split when the caller supplied no per-clip
        timing at all, which is the pre-beat behavior.
        """
        clips = segment.visual_clips
        supplied = [clip.duration_sec for clip in clips]
        if any(value is None or value <= 0 for value in supplied):
            return [duration / len(clips)] * len(clips)
        total = sum(supplied)
        if total <= 0:
            return [duration / len(clips)] * len(clips)
        return [value * duration / total for value in supplied]

    def _normalize_branding(
        self, work_dir: str, name: str, data: bytes, width: int, height: int, profile: RenderProfile
    ) -> str:
        if not data:
            raise EditorInputError(f"{name}: branding asset has no data")
        src = self._write_temp(work_dir, f"{name}_src.mp4", data)
        self._validate_decodable(src, f"{name} (branding asset)")
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run(
            [
                self._ffmpeg, "-y", "-i", src, "-vf", self._scale_pad_filter(width, height, profile.fps),
                "-r", str(profile.fps), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", self._audio_bitrate, out,
            ],
            stage=f"normalize branding ({name})",
        )
        return out

    def _normalize_visual(
        self,
        work_dir: str,
        name: str,
        data: bytes,
        kind: str,
        duration: float,
        width: int,
        height: int,
        profile: RenderProfile,
        *,
        zoom_in: bool = True,
    ) -> str:
        if not data:
            raise EditorInputError(f"{name}: no data provided for a {kind} visual clip")
        extension = "png" if kind == "image" else "mp4"
        src = self._write_temp(work_dir, f"{name}_src.{extension}", data)
        # `-loop 1` (used below for images) can hang indefinitely on
        # genuinely corrupt image bytes instead of failing fast, unlike a
        # plain non-looping decode of the same file — validate first so
        # corrupt media is rejected quickly and honestly as an
        # EditorInputError rather than only surfacing after the full
        # configured ffmpeg timeout elapses.
        self._validate_decodable(src, f"{name} ({kind} visual clip)")
        out = os.path.join(work_dir, f"{name}.mp4")

        if kind == "image":
            input_args = ["-loop", "1"]
            vf = self._still_image_filter(width, height, duration, profile, zoom_in=zoom_in)
        else:
            # Deliberately *not* `-stream_loop -1`: looping a short clip
            # to fill a longer slot replays the same motion over and
            # over, which is exactly the visible repetition this
            # pipeline exists to avoid. `tpad=stop_mode=clone` instead
            # holds the final frame for however long is left, so a clip
            # shorter than its slot ends on a still rather than
            # restarting. A clip longer than its slot is still trimmed
            # by `-t`.
            input_args = []
            vf = (
                f"{self._scale_pad_filter(width, height, profile.fps)},"
                f"tpad=stop_mode=clone:stop_duration={duration:.3f}"
            )

        self._run(
            [
                self._ffmpeg, "-y", *input_args, "-i", src, "-t", f"{duration:.3f}",
                "-vf", vf, "-pix_fmt", "yuv420p", "-an", out,
            ],
            stage=f"normalize visual ({name})",
        )
        return out

    @staticmethod
    def _still_image_filter(
        width: int, height: int, duration: float, profile: RenderProfile, *, zoom_in: bool
    ) -> str:
        """Scale/pad a still to the output frame, then apply a slow Ken
        Burns push so it is never completely motionless.

        The image is first scaled up by the total zoom the move will
        consume, so `zoompan` crops *into* real pixels rather than
        magnifying the output frame — without that pre-scale the effect
        visibly softens the image as it pushes in.
        """
        fps = profile.fps
        if profile.ken_burns_zoom_per_sec <= 0 or duration <= 0:
            return f"{FFmpegEditorProvider._scale_pad_filter(width, height, fps)}"

        total_frames = max(int(round(duration * fps)), 1)
        # Total additional scale across the whole clip, clamped so a long
        # beat can't push so far that the framing changes drastically.
        zoom_span = min(profile.ken_burns_zoom_per_sec * duration, 0.25)
        max_zoom = 1.0 + zoom_span
        # `on` is zoompan's current output frame index.
        progress = f"(on/{max(total_frames - 1, 1)})"
        zoom_expr = (
            f"{1.0:.4f}+{zoom_span:.4f}*{progress}"
            if zoom_in
            else f"{max_zoom:.4f}-{zoom_span:.4f}*{progress}"
        )
        supersample_w = int(width * (max_zoom + 0.05))
        supersample_h = int(height * (max_zoom + 0.05))
        return (
            f"scale={supersample_w}:{supersample_h}:force_original_aspect_ratio=increase,"
            f"crop={supersample_w}:{supersample_h},"
            f"zoompan=z='{zoom_expr}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s={width}x{height}:fps={fps},"
            f"setsar=1"
        )

    def _black_clip(self, work_dir: str, name: str, duration: float, width: int, height: int, profile: RenderProfile) -> str:
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run(
            [
                self._ffmpeg, "-y", "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}:d={duration:.3f}:r={profile.fps}",
                "-pix_fmt", "yuv420p", out,
            ],
            stage=f"black clip ({name})",
        )
        return out

    def _normalize_audio(self, work_dir: str, name: str, data: bytes, duration: float, description: str) -> str:
        if not data:
            raise EditorInputError(f"{description}: no audio data provided")
        src = self._write_temp(work_dir, f"{name}_src.mp3", data)
        self._validate_decodable(src, description)
        out = os.path.join(work_dir, f"{name}.wav")
        self._run(
            [
                self._ffmpeg, "-y", "-i", src, "-af", "apad", "-t", f"{duration:.3f}",
                "-ar", "44100", "-ac", "2", out,
            ],
            stage=f"normalize audio ({name})",
        )
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
        self._run(cmd, stage=f"mix audio ({name})")
        return out

    def _concat_visuals(
        self, work_dir: str, name: str, paths: list[str], profile: RenderProfile
    ) -> str:
        """Join one segment's visuals into a single clip, dissolving
        between them rather than hard-cutting.

        A dissolve consumes overlap: `xfade` starts the next clip
        `visual_crossfade_sec` before the previous one ends, so N clips
        joined this way are shorter than their sum by (N-1) x the
        dissolve. `_build_segment` has already rendered each clip that
        much longer to compensate, so the joined track lands back on the
        segment's real duration.
        """
        if len(paths) == 1:
            return paths[0]

        durations = [self._probe_duration(path) for path in paths]
        fade = self._visual_fade(profile, durations)
        if fade <= 0:
            return self._concat(work_dir, name, paths)

        cmd = [self._ffmpeg, "-y"]
        for path in paths:
            cmd += ["-i", path]

        # Chain xfades left to right, tracking where each successive
        # transition starts on the *accumulated* timeline.
        filters = []
        current = "[0:v]"
        elapsed = durations[0]
        for index in range(1, len(paths)):
            offset = max(elapsed - fade, 0.0)
            label = f"[x{index}]"
            filters.append(
                f"{current}[{index}:v]xfade=transition=fade:duration={fade:.3f}:"
                f"offset={offset:.3f}{label}"
            )
            current = label
            elapsed = offset + fade + (durations[index] - fade)

        total = sum(durations) - fade * (len(paths) - 1)
        out = os.path.join(work_dir, f"{name}.mp4")
        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", current,
            "-t", f"{total:.3f}",
            "-r", str(profile.fps), "-pix_fmt", "yuv420p", "-an", out,
        ]
        self._run(cmd, stage=f"crossfade visuals ({name})")
        return out

    def _concat(self, work_dir: str, name: str, paths: list[str]) -> str:
        if len(paths) == 1:
            return paths[0]
        list_path = os.path.join(work_dir, f"{name}_list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for path in paths:
                f.write(f"file '{path}'\n")
        out = os.path.join(work_dir, f"{name}.mp4")
        self._run(
            [self._ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out],
            stage=f"concat ({name})",
        )
        return out

    # --- whole-video background music --------------------------------------

    def _mix_background_music(
        self, work_dir: str, assembled_path: str, music_data: bytes, total_duration_sec: float
    ) -> str:
        if not music_data:
            raise EditorInputError("background_music was provided but contained no data")
        music_src = self._write_temp(work_dir, "music_src.mp3", music_data)
        self._validate_decodable(music_src, "background_music")
        music_prepared = os.path.join(work_dir, "music_prepared.wav")
        self._run(
            [
                self._ffmpeg, "-y", "-stream_loop", "-1", "-i", music_src,
                "-t", f"{max(total_duration_sec, 0.1):.3f}",
                "-af", f"volume={self._background_music_volume}",
                "-ar", "44100", "-ac", "2", music_prepared,
            ],
            stage="prepare background music",
        )
        out = os.path.join(work_dir, "with_music.mp4")
        self._run(
            [
                self._ffmpeg, "-y", "-i", assembled_path, "-i", music_prepared,
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", self._audio_bitrate, out,
            ],
            stage="mix background music",
        )
        return out

    # --- finalize: loudness normalization + subtitle burn-in ---------------

    def _finalize(
        self, work_dir: str, assembled_path: str, subtitle_lines: list[SubtitleLine], profile: RenderProfile
    ) -> str:
        vf_parts = [
            f"{self._drawtext_filter(work_dir, f'subtitle{i}', cue.text, profile.subtitle_font_size, emphasized=cue.emphasized)}"
            f":enable='between(t,{cue.start_sec:.3f},{cue.end_sec:.3f})'"
            for i, cue in enumerate(subtitle_lines)
        ]
        vf_chain = ",".join(vf_parts) if vf_parts else "null"
        # Single-pass EBU R128 loudnorm: real and working, though less
        # precise than a two-pass analyze-then-normalize approach (which
        # would need a first ffmpeg pass just to measure loudness before
        # a second pass applies it) — a deliberate simplicity/accuracy
        # tradeoff, not an oversight.
        af_chain = f"loudnorm=I={profile.loudness_target_lufs}:TP=-1.5:LRA=11"

        out = os.path.join(work_dir, "final.mp4")
        filter_complex = f"[0:v]{vf_chain}[v];[0:a]{af_chain}[a]"
        self._run(
            [
                self._ffmpeg, "-y", "-i", assembled_path,
                "-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
                "-r", str(profile.fps), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", self._audio_bitrate, out,
            ],
            stage="finalize (loudnorm + subtitles)",
        )
        return out

    def _drawtext_filter(
        self, work_dir: str, key: str, text: str, font_size: int, *, y_offset: int = 0, emphasized: bool = False
    ) -> str:
        # `textfile=` rather than an inline `text='...'` literal: an
        # inline literal needs backslash-escaping for quotes, which
        # breaks once it sits inside a larger comma/colon-delimited
        # `-filter_complex` string alongside other filters (discovered
        # empirically with apostrophes in narration text — the escaped
        # quote corrupted the outer filtergraph parser's tokenization).
        # Writing the text to its own file sidesteps the whole class of
        # escaping bugs since the content itself never appears in the
        # filtergraph description.
        color = "yellow" if emphasized else "white"
        text_path = self._write_temp(work_dir, f"drawtext_{key}.txt", text.encode("utf-8"))
        escaped_font = self._escape_for_filter(self._font_path)
        escaped_textfile = self._escape_for_filter(text_path)
        return (
            f"drawtext=fontfile={escaped_font}:textfile={escaped_textfile}:fontsize={font_size}:"
            f"fontcolor={color}:borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=h-160-{y_offset}"
        )

    @staticmethod
    def _scale_pad_filter(width: int, height: int, fps: int) -> str:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )

    def _probe_duration(self, path: str) -> float:
        try:
            result = subprocess.run(
                [self._ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._ffmpeg_timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise EditorTimeoutError(
                f"ffprobe timed out after {self._ffmpeg_timeout_sec:.0f}s probing {path}"
            ) from exc
        if result.returncode != 0:
            raise EditorRenderError(
                f"ffprobe failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
            )
        try:
            return float(result.stdout.decode("utf-8").strip())
        except ValueError as exc:
            raise EditorRenderError(
                f"ffprobe returned a non-numeric duration for {path}: {result.stdout!r}"
            ) from exc

    def _run(self, cmd: list[str], *, stage: str) -> None:
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._ffmpeg_timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise EditorTimeoutError(
                f"ffmpeg timed out after {self._ffmpeg_timeout_sec:.0f}s during {stage!r}: {' '.join(cmd)}"
            ) from exc
        if result.returncode != 0:
            raise EditorRenderError(
                f"ffmpeg command failed during {stage!r} (exit {result.returncode}): {' '.join(cmd)}\n"
                f"{result.stderr.decode('utf-8', errors='replace')[-4000:]}"
            )

    def _validate_decodable(self, path: str, description: str) -> None:
        """Fast pre-flight decode check, ahead of the real normalize
        commands. Discovered empirically: `-loop 1` (used to loop a
        still image) can hang *indefinitely* on genuinely corrupt image
        bytes rather than failing fast, unlike a plain, non-looping
        single-frame decode of the same file — this catches that (and
        any other undecodable input) quickly and reports it honestly as
        `EditorInputError`, instead of only surfacing after the full
        `ffmpeg_timeout_sec` budget elapses.
        """
        validation_timeout = min(self._ffmpeg_timeout_sec, 15.0)
        try:
            result = subprocess.run(
                [self._ffmpeg, "-y", "-i", path, "-frames:v", "1", "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=validation_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EditorInputError(
                f"{description}: media could not be validated within {validation_timeout:.0f}s "
                "(likely corrupt or unsupported)"
            ) from exc
        if result.returncode != 0:
            raise EditorInputError(
                f"{description}: corrupt or unsupported media "
                f"({result.stderr.decode('utf-8', errors='replace')[-1000:]})"
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
