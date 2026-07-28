"""ComfyUI image generation provider — a local, self-hosted, free
alternative to a paid image-gen vendor. Used by the Video Agent's Asset
Generation module (for `ai_image`/`diagram`/`map`/`portrait` asset
requirements) and Thumbnail Generation (thumbnail candidates, always at
1280x720 — see thumbnail_agent.py). Talks to a local ComfyUI Desktop
instance over its documented HTTP API via
`libs/providers/_comfyui_common.py` — the same client
`video_gen.comfyui_provider.ComfyUIVideoProvider` uses, since the
submit/poll/fetch-output mechanics are identical; see that module's own
docstring for the full rationale on why no workflow is hardcoded here.
"""

import random
import uuid
from typing import Any

from libs.core.config import get_settings

from .._comfyui_common import ComfyUIClient, load_workflow_template, render_workflow
from .base import ImageGenProvider

_IMAGE_OUTPUT_KEYS = ("images",)


class ComfyUIImageProvider(ImageGenProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        settings = get_settings()
        base_url = config.get("base_url") or settings.comfyui_url
        self._client = ComfyUIClient(
            base_url=base_url,
            timeout_sec=float(config.get("timeout_sec", 30)),
            poll_interval_sec=float(config.get("poll_interval_sec", 2)),
            poll_timeout_sec=float(config.get("poll_timeout_sec", 300)),
            max_attempts=int(config.get("max_attempts", 3)),
            retry_backoff_sec=float(config.get("retry_backoff_sec", 2)),
        )
        template_path = config.get("workflow_template", "config/comfyui/text_to_image.json")
        self._workflow, self._output_node_title = load_workflow_template(template_path)
        #: 1280x720 by default — every caller of this provider today
        #: (Asset Generation's ai_image/diagram/map/portrait
        #: requirements, and Thumbnail Generation) wants a 16:9 frame;
        #: a future caller can still override per-call via kwargs.
        self._width = int(config.get("width", 1280))
        self._height = int(config.get("height", 720))
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
            "FILENAME_PREFIX": f"ytagent_{uuid.uuid4().hex[:10]}",
        }
        workflow = render_workflow(self._workflow, substitutions)
        return self._client.generate(
            workflow, output_node_title=self._output_node_title, media_keys=_IMAGE_OUTPUT_KEYS
        )
