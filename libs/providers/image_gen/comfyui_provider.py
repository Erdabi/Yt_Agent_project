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

#: Used whenever a caller doesn't supply its own `negative_prompt`.
#: Names the *generator's* own failure modes (mangled lettering, anatomy
#: artifacts, low-detail mush) rather than any subject matter, so it
#: stays correct whatever is being generated.
_DEFAULT_NEGATIVE_PROMPT = (
    "text, watermark, logo, signature, caption, letters, words, "
    "blurry, out of focus, low resolution, low detail, jpeg artifacts, noise, "
    "washed out, flat lighting, deformed, disfigured, extra limbs, extra fingers, "
    "mutated hands, bad anatomy, ugly, amateur, poorly drawn, cropped"
)


def _round_to_multiple_of_eight(value: float) -> int:
    """Latent-space dimensions must be multiples of 8 (a latent pixel is
    an 8x8 image block), and ComfyUI rejects anything else outright.
    """
    return max(8, int(round(value / 8)) * 8)


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
        #: The resolution the *diffusion model itself* generates at, not
        #: the final output size — see `upscale_factor` below. Kept at a
        #: size the model is actually trained for: pushing a base pass
        #: far past that is the classic cause of duplicated heads and
        #: repeated horizons, so detail is added by the hires pass
        #: instead. 16:9 by default, since every caller of this provider
        #: (Asset Generation's ai_image/diagram/map/portrait
        #: requirements, and Thumbnail Generation) wants a 16:9 frame.
        self._width = int(config.get("width", 768))
        self._height = int(config.get("height", 432))
        #: How much the hires pass enlarges the base render. The second
        #: sampler pass then re-diffuses at that larger size, which is
        #: what actually adds detail rather than just interpolating
        #: pixels — the single biggest quality lever in this workflow.
        #: 2x (768x432 -> 1536x864) rather than a factor large enough to
        #: hit 1080p directly: the hires pass diffuses at the upscaled
        #: size, so the same coherence ceiling that caps the base pass
        #: applies again there, and pushing it to 1920x1080 on an
        #: SD1.5-class checkpoint was measured as both incoherent and
        #: prohibitively slow. The compositor's final 1.25x resize to
        #: 1080p costs far less than an incoherent render would.
        self._upscale_factor = float(config.get("upscale_factor", 2.0))
        self._steps = int(config.get("steps", 28))
        #: Fewer steps suffice for the hires pass because it starts from
        #: an already-formed image rather than from noise.
        self._hires_steps = int(config.get("hires_steps", 12))
        #: How much freedom the hires pass has to change the base image.
        #: High enough to synthesize real detail, low enough to keep the
        #: composition the base pass established.
        self._hires_denoise = float(config.get("hires_denoise", 0.45))
        #: dpmpp_2m + karras: the widely-used default pairing for
        #: detailed, low-artifact output at modest step counts — a clear
        #: upgrade on plain `euler`/`normal`, which needs far more steps
        #: to reach comparable detail.
        self._sampler = config.get("sampler", "dpmpp_2m")
        self._scheduler = config.get("scheduler", "karras")
        #: Prompt adherence. Below ~5 drifts off-prompt; above ~9 burns
        #: contrast and oversaturates.
        self._cfg = float(config.get("cfg", 6.5))
        self._seed = config.get("seed")
        self._negative_prompt = config.get("negative_prompt", _DEFAULT_NEGATIVE_PROMPT)

    def generate(self, prompt: str, **kwargs: Any) -> bytes:
        seed = kwargs.get("seed", self._seed)
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        width = int(kwargs.get("width", self._width))
        height = int(kwargs.get("height", self._height))
        upscale_factor = float(kwargs.get("upscale_factor", self._upscale_factor))
        substitutions: dict[str, Any] = {
            "PROMPT": prompt,
            "NEGATIVE_PROMPT": kwargs.get("negative_prompt") or self._negative_prompt,
            "WIDTH": width,
            "HEIGHT": height,
            # Latent dimensions must be multiples of 8; rounding here
            # rather than trusting the configured factor to divide
            # evenly keeps any width/upscale_factor combination valid.
            "UPSCALE_WIDTH": _round_to_multiple_of_eight(width * upscale_factor),
            "UPSCALE_HEIGHT": _round_to_multiple_of_eight(height * upscale_factor),
            "SEED": int(seed),
            "STEPS": int(kwargs.get("steps", self._steps)),
            "HIRES_STEPS": int(kwargs.get("hires_steps", self._hires_steps)),
            "HIRES_DENOISE": float(kwargs.get("hires_denoise", self._hires_denoise)),
            "SAMPLER": kwargs.get("sampler") or self._sampler,
            "SCHEDULER": kwargs.get("scheduler") or self._scheduler,
            "CFG": float(kwargs.get("cfg", self._cfg)),
            "FILENAME_PREFIX": f"ytagent_{uuid.uuid4().hex[:10]}",
        }
        workflow = render_workflow(self._workflow, substitutions)
        return self._client.generate(
            workflow, output_node_title=self._output_node_title, media_keys=_IMAGE_OUTPUT_KEYS
        )
