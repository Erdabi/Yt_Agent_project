"""Audio library provider interface.

A concrete implementation sources one supplementary audio clip — a sound
effect or a background music cue — from a library or generator, used by
the Video Agent's Asset Generation module
(services/agent_video/app/modules/asset_generation.py) for
`sound_effect`/`background_music_cue`-type asset requirements. Distinct
from `tts` (libs/providers/tts/): this is incidental supplementary audio
layered under narration, never the narration itself. No real
implementation exists yet — see stub_provider.py and
docs/architecture/06-roadmap.md, Phase 1.
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class AudioLibraryProvider(Provider):
    @abstractmethod
    def search(self, description: str, **kwargs: Any) -> bytes:
        """Search for audio matching `description` and return the best
        match's raw bytes.
        """
