"""`VideoAgent` — the single agent the Manager dispatches for the whole
`video_creation` stage.

Storyboard, voice-over, video assembly, and thumbnail generation used to
each be their own agent, queue, and `ProjectStage`. They are now four
modules run in sequence inside one `run()` call, so one `Job` row and one
Manager decision covers all four:

    storyboard -> voiceover -> assembly -> thumbnail

That order is a real dependency chain, not an arbitrary one: assembly
needs both the storyboard's shot list and the voiceover's audio/timing to
composite a render, so it can only run after both. Thumbnail has no such
dependency (see modules/thumbnail.py) but runs last anyway to keep this
method a single straight-line sequence.

The trade-off this consolidation makes explicit: a failure partway
through (e.g. assembly breaks) means the *whole* video job is retried by
the Manager, including the already-succeeded storyboard and voiceover
work, not just the broken module. That's an acceptable cost here — those
modules are fast/cheap compared to assembly, and a partial-video retry
was never really "resume where it broke" anyway, since assembly's output
depends on both of them regardless.
"""

from typing import Any

from libs.agents.base import BaseAgent
from libs.schemas.jobs import JobContext

from .modules.assembly import AssemblyModule
from .modules.storyboard import StoryboardModule
from .modules.thumbnail import ThumbnailModule
from .modules.voiceover import VoiceoverModule


class VideoAgent(BaseAgent):
    name = "video"

    def __init__(self) -> None:
        self._storyboard = StoryboardModule()
        self._voiceover = VoiceoverModule()
        self._assembly = AssemblyModule()
        self._thumbnail = ThumbnailModule()

    def run(self, context: JobContext) -> dict[str, Any]:
        storyboard_result = self._storyboard.plan_shots(context)
        voiceover_result = self._voiceover.synthesize(context)
        assembly_result = self._assembly.render(context, storyboard_result, voiceover_result)
        thumbnail_result = self._thumbnail.generate(context)
        return {
            "storyboard": storyboard_result,
            "voiceover": voiceover_result,
            "assembly": assembly_result,
            "thumbnail": thumbnail_result,
        }
