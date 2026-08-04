# ComfyUI workflow templates

Each `*.json` file here is a small wrapper around a ComfyUI graph exported
via ComfyUI's own **"Save (API Format)"** menu option — not something this
codebase hardcodes or interprets beyond the two conventions below. This is
what `config/providers.yaml`'s `video_gen.comfyui.config.workflow_template`
and `image_gen.comfyui.config.workflow_template` point at
(`libs/providers/{video_gen,image_gen}/comfyui_provider.py`, sharing
`libs/providers/_comfyui_common.py`'s loader/renderer).

```json
{
  "output_node_title": "SaveImage",
  "workflow": { "...": "the exported API-format graph, verbatim" }
}
```

- **`workflow`** — paste your own exported graph here unmodified, except
  for the placeholder tokens below.
- **`output_node_title`** — the ComfyUI node whose produced media this
  provider fetches. Set a node's *Title* in the ComfyUI UI (right-click a
  node -> Title) before exporting, and put that exact string here. If
  omitted, the provider falls back to the first node in the graph whose
  output contains an image/video file.

## Placeholder convention

Anywhere the provider should fill in a value at generation time, write a
`{{TOKEN}}` string in that field instead of a literal value:

| Token | Type | Meaning |
|---|---|---|
| `{{PROMPT}}` | string | The generation prompt text |
| `{{NEGATIVE_PROMPT}}` | string | What to steer away from |
| `{{WIDTH}}` / `{{HEIGHT}}` | integer | **Base** generation dimensions — what the model diffuses at, not the final output size |
| `{{UPSCALE_WIDTH}}` / `{{UPSCALE_HEIGHT}}` | integer | Final output dimensions, after the hires pass |
| `{{SEED}}` | integer | Sampler seed |
| `{{STEPS}}` | integer | Base-pass sampler step count |
| `{{HIRES_STEPS}}` | integer | Hires-pass step count (fewer — it starts from a formed image, not noise) |
| `{{HIRES_DENOISE}}` | float | How much freedom the hires pass has to change the base image (0-1) |
| `{{SAMPLER}}` / `{{SCHEDULER}}` | string | Sampler and schedule names, e.g. `dpmpp_2m` / `karras` |
| `{{CFG}}` | float | Prompt-adherence strength |
| `{{FRAME_COUNT}}` | integer | Video only — how many frames to generate |
| `{{FILENAME_PREFIX}}` | string | A unique prefix so concurrent jobs never collide on disk |

A field whose value is *exactly* one token (e.g. `"width": "{{WIDTH}}"`)
is replaced with the real typed value (an `int`, matching what ComfyUI's
own node validation expects) — not a stringified one. A token embedded
inside a larger string (e.g. `"filename_prefix": "shot_{{SEED}}"`) is
substituted as text instead, since that field has to stay a string
either way. See `render_workflow` in `_comfyui_common.py` for the exact
mechanics.

## The templates shipped here

- **`text_to_image.json`** — a txt2img graph with a **hires second
  pass**, using core nodes only — no custom node installs required.
  Point `ckpt_name` (node `"4"`) at whatever checkpoint is actually
  loaded in your ComfyUI Desktop instance.

  The graph is: `CheckpointLoaderSimple` -> `CLIPTextEncode` (positive
  and negative) -> `EmptyLatentImage` -> `KSampler` (base) ->
  `VAEDecode` -> `ImageScale` -> `VAEEncode` -> `KSampler` (hires, at
  reduced denoise) -> `VAEDecode` -> `SaveImage`.

  Two things about that shape are deliberate and worth preserving if
  you swap in your own graph:

  1. **The base pass renders small and the hires pass enlarges it.**
     Diffusing directly at output resolution is what produces duplicated
     faces and repeated horizons — every checkpoint has a resolution it
     was trained at, and coherence falls apart well before 1080p on
     SD1.5-class models. Generating near that trained size and then
     re-diffusing the enlarged result adds real detail instead.
  2. **The upscale happens in pixel space** (`VAEDecode` ->
     `ImageScale` -> `VAEEncode`), not with `LatentUpscale`. Measured
     here: upscaling the latent with `nearest-exact` and re-diffusing at
     a moderate denoise left a pronounced grid/checkerboard pattern
     across the whole frame, because the interpolated latent carries
     block structure the second pass doesn't fully overwrite. The extra
     VAE round-trip costs time but removes that artifact class
     entirely.

- **`text_to_video.json`** — **a starting-point example, not a verified
  graph.** It targets the AnimateDiff-Evolved + VideoHelperSuite combo
  (`ADE_AnimateDiffLoaderWithContext` + `VHS_VideoCombine`), the most
  commonly documented community text-to-video pipeline — both
  installable in one click via ComfyUI Manager — but node names/fields
  in that combo (and in any of the several other valid approaches: native
  Stable Video Diffusion, Mochi, LTX-Video, or Hunyuan Video node
  pipelines ComfyUI core has added at various points) shift with that
  extension's own releases and with which checkpoint/motion-module you
  actually have installed. **Before relying on this for a real render**:
  build the equivalent graph in your own ComfyUI Desktop, confirm it runs
  there, export it via "Save (API Format)", and drop it in here in place
  of this file (keeping the same `{{PLACEHOLDER}}`/`output_node_title`
  conventions) — exactly the point of this being swappable template data
  rather than code.

## Why a wrapper instead of a bare ComfyUI graph

Keeping `output_node_title` alongside `workflow` (rather than, say,
inferring the output node from graph topology) means this codebase never
has to guess which of possibly several `SaveImage`/`VHS_VideoCombine`-like
nodes in a more complex graph is the one whose output matters — you say
so once, explicitly, when you build the workflow.
