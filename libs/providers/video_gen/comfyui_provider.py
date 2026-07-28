"""ComfyUI video generation provider — a local, self-hosted, free
alternative to Runway, for AI b-roll (`ai_video`/`animation` asset
requirements) when no paid video-gen vendor account exists. Talks to a
local ComfyUI Desktop instance
(https://github.com/comfyanonymous/ComfyUI) over its documented HTTP API
via `libs/providers/_comfyui_common.py` — the same client both
`video_gen` and `image_gen`'s ComfyUI providers share, since submitting/
polling/fetching-output mechanics are identical between them.

Does not hardcode a workflow: which nodes/models/samplers actually run is
entirely up to whatever graph the user has built and exported (ComfyUI
Desktop -> "Save (API Format)") into a `config/comfyui/*.json` template —
this provider only fills in a handful of `{{PLACEHOLDER}}` values that
template's own text/seed/dimension inputs use (see
`_comfyui_common.render_workflow`) and reads back whichever output node
produced media. Swapping to a different checkpoint/workflow entirely is
editing that JSON template (or pointing `workflow_template` at a
different one in config/providers.yaml), never a code change here.
"""

import random
import uuid
from typing import Any

from libs.core.config import get_settings

from .._comfyui_common import ComfyUIClient, load_workflow_template, render_workflow
from .base import VideoGenProvider

#: ComfyUI's community video-combine nodes (e.g. VHS_VideoCombine)
#: report their output under `"gifs"` regardless of actual
#: container/codec; a small number of newer/custom nodes use
#: `"videos"` — tried in that order.
_VIDEO_OUTPUT_KEYS = ("gifs", "videos")


class ComfyUIVideoProvider(VideoGenProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        settings = get_settings()
        base_url = config.get("base_url") or settings.comfyui_url
        self._client = ComfyUIClient(
            base_url=base_url,
            timeout_sec=float(config.get("timeout_sec", 30)),
            poll_interval_sec=float(config.get("poll_interval_sec", 3)),
            poll_timeout_sec=float(config.get("poll_timeout_sec", 900)),
            max_attempts=int(config.get("max_attempts", 3)),
            retry_backoff_sec=float(config.get("retry_backoff_sec", 2)),
        )
        template_path = config.get("workflow_template", "config/comfyui/text_to_video.json")
        self._workflow, self._output_node_title = load_workflow_template(template_path)
        self._width = int(config.get("width", 1280))
        self._height = int(config.get("height", 720))
        self._frame_count = int(config.get("frame_count", 65))
        self._steps = int(config.get("steps", 20))
        self._seed = config.get("seed")

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        seed = kwargs.get("seed", self._seed)
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        substitutions: dict[str, Any] = {
            "PROMPT": prompt,
            "WIDTH": int(kwargs.get("width", self._width)),
            "HEIGHT": int(kwargs.get("height", self._height)),
            "SEED": int(seed),
            "STEPS": int(kwargs.get("steps", self._steps)),
            "FRAME_COUNT": int(kwargs.get("frame_count", self._frame_count)),
            "FILENAME_PREFIX": f"ytagent_{uuid.uuid4().hex[:10]}",
        }
        workflow = render_workflow(self._workflow, substitutions)
        return self._client.generate(
            workflow, output_node_title=self._output_node_title, media_keys=_VIDEO_OUTPUT_KEYS
        )
