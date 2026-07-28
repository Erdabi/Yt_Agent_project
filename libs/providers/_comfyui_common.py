"""Shared ComfyUI HTTP client + workflow-template plumbing for
`video_gen`/`image_gen`'s ComfyUI providers (comfyui_provider.py in each
of those capability folders). Both providers talk to the same local
ComfyUI Desktop instance (https://github.com/comfyanonymous/ComfyUI —
its documented HTTP API) the same way, differing only in *which*
workflow template they load and which output node's media they're after
— so that shared mechanics (submit, poll, fetch bytes, placeholder
substitution) live here once instead of twice.

Workflow templates (config/comfyui/*.json) are never ComfyUI graphs
hardcoded into this codebase — see each capability's own module
docstring for why. A template is a small wrapper around an exported
ComfyUI "Save (API Format)" graph:

    {
      "output_node_title": "SaveImage",
      "workflow": { "3": {"class_type": "KSampler", "inputs": {...}}, ... }
    }

`workflow` is whatever graph a user's own ComfyUI Desktop produces,
completely opaque to this codebase, except for one convention this
codebase relies on: any input value the caller should fill in at
generation time is written as a `{{PLACEHOLDER}}` token (e.g. a
CLIPTextEncode node's `text` set to `"{{PROMPT}}"`) — `render_workflow`
below does a plain string substitution over every string leaf in the
graph, nothing more. `output_node_title` names the node (via ComfyUI's
own optional per-node `_meta.title`, set in the ComfyUI UI before
exporting) whose produced media this client fetches; omitted, it falls
back to the first node in the graph whose output contains any of the
media keys the caller is looking for.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ComfyUIAPIError(RuntimeError):
    """ComfyUI rejected a workflow outright, a queued job failed, or a
    request failed after exhausting its retry budget — distinct from a
    transient network/5xx error (already retried internally). Raised
    honestly rather than swallowed, matching every other provider's
    error handling in this codebase.
    """


def load_workflow_template(path: str) -> tuple[dict[str, Any], str | None]:
    """Loads a `config/comfyui/*.json` template and returns
    `(workflow_graph, output_node_title)`. Relative paths resolve
    against the repo root, the same convention
    `libs/providers/registry.py`'s own config-file loading uses.
    """
    template_path = Path(path)
    if not template_path.is_absolute():
        template_path = _REPO_ROOT / path
    if not template_path.is_file():
        raise ComfyUIAPIError(f"ComfyUI workflow template not found: {template_path}")
    with template_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    workflow = data.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        raise ComfyUIAPIError(f"ComfyUI workflow template has no 'workflow' graph: {template_path}")
    return workflow, data.get("output_node_title")


def render_workflow(workflow: dict[str, Any], substitutions: dict[str, Any]) -> dict[str, Any]:
    """Returns a deep copy of `workflow` with every `{{KEY}}` token
    resolved from `substitutions` — operating on the already-parsed
    Python structure (not raw JSON text) so a substituted value
    containing quotes/braces can never corrupt the surrounding graph
    the way a naive text-level find/replace could.

    ComfyUI validates node inputs by type — `EmptyLatentImage.width`,
    `KSampler.seed`, etc. are integers, not strings — so a string leaf
    that is *exactly* one placeholder token (e.g. `"width": "{{WIDTH}}"`
    in the template) is replaced with `substitutions["WIDTH"]` as-is,
    preserving whatever type the caller put there (an `int`, not
    `str(int)`). A placeholder embedded inside a larger string (e.g.
    `"filename_prefix": "shot_{{SEED}}"`) is stringified in place instead,
    since the field itself must stay a string either way.
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: _walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            for key, value in substitutions.items():
                token = f"{{{{{key}}}}}"
                if node == token:
                    return value
                if token in node:
                    node = node.replace(token, str(value))
            return node
        return node

    return _walk(workflow)


def _find_titled_node_id(workflow: dict[str, Any], title: str) -> str | None:
    for node_id, node in workflow.items():
        meta = node.get("_meta") if isinstance(node, dict) else None
        if isinstance(meta, dict) and meta.get("title") == title:
            return node_id
    return None


class ComfyUIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_sec: float = 30,
        poll_interval_sec: float = 2,
        poll_timeout_sec: float = 600,
        max_attempts: int = 3,
        retry_backoff_sec: float = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._poll_interval_sec = poll_interval_sec
        self._poll_timeout_sec = poll_timeout_sec
        self._max_attempts = max_attempts
        self._retry_backoff_sec = retry_backoff_sec

    def generate(
        self, workflow: dict[str, Any], *, output_node_title: str | None, media_keys: tuple[str, ...]
    ) -> bytes:
        """Submits `workflow`, blocks until ComfyUI finishes it, and
        returns the produced media's raw bytes. `media_keys` is tried in
        order against each candidate node's outputs (e.g. `("images",)`
        for a still image, `("gifs", "videos")` for a video — ComfyUI's
        community video-combine nodes commonly report their output under
        `"gifs"` regardless of actual container/codec).
        """
        node_id = _find_titled_node_id(workflow, output_node_title) if output_node_title else None
        prompt_id = self._submit(workflow)
        history_entry = self._poll_until_complete(prompt_id)
        return self._fetch_output(history_entry, prompt_id, node_id, media_keys)

    # --- submit -------------------------------------------------------------

    def _submit(self, workflow: dict[str, Any]) -> str:
        body = {"prompt": workflow, "client_id": uuid.uuid4().hex}
        response = self._request_with_retry("POST", f"{self._base_url}/prompt", json=body)
        data = self._json(response)

        node_errors = data.get("node_errors") or {}
        if node_errors:
            raise ComfyUIAPIError(f"ComfyUI rejected the workflow (node_errors): {node_errors}")
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyUIAPIError(f"ComfyUI /prompt returned no prompt_id: {data!r}")
        return prompt_id

    # --- poll -----------------------------------------------------------------

    def _poll_until_complete(self, prompt_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._poll_timeout_sec
        while True:
            response = self._request_with_retry("GET", f"{self._base_url}/history/{prompt_id}")
            data = self._json(response)
            entry = data.get(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyUIAPIError(f"ComfyUI job {prompt_id} failed: {status}")
                return entry

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ComfyUI job {prompt_id} did not finish within {self._poll_timeout_sec:.0f}s"
                )
            time.sleep(self._poll_interval_sec)

    # --- fetch output -----------------------------------------------------------

    def _fetch_output(
        self,
        history_entry: dict[str, Any],
        prompt_id: str,
        node_id: str | None,
        media_keys: tuple[str, ...],
    ) -> bytes:
        outputs = history_entry.get("outputs") or {}
        if node_id is not None:
            candidates = [outputs.get(node_id) or {}]
        else:
            candidates = list(outputs.values())

        for node_output in candidates:
            for key in media_keys:
                files = node_output.get(key)
                if files:
                    ref = files[0]
                    return self._download(ref)

        raise ComfyUIAPIError(
            f"ComfyUI job {prompt_id} completed but produced no output under {media_keys!r} "
            f"(node_id={node_id!r}, outputs keys={list(outputs)!r})"
        )

    def _download(self, ref: dict[str, Any]) -> bytes:
        params = {
            "filename": ref["filename"],
            "subfolder": ref.get("subfolder", ""),
            "type": ref.get("type", "output"),
        }
        response = self._request_with_retry("GET", f"{self._base_url}/view", params=params)
        return response.content

    # --- HTTP plumbing -------------------------------------------------------

    def _request_with_retry(
        self, method: str, url: str, *, json: dict | None = None, params: dict | None = None
    ) -> requests.Response:
        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            try:
                response = requests.request(
                    method, url, json=json, params=params, timeout=self._timeout_sec
                )
            except requests.exceptions.RequestException as exc:
                if is_last_attempt:
                    raise ComfyUIAPIError(
                        f"ComfyUI {method} {url} failed after {attempt} attempts: {exc} — is "
                        f"ComfyUI running at {self._base_url!r}?"
                    ) from exc
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 500:
                if is_last_attempt:
                    raise ComfyUIAPIError(
                        f"ComfyUI {method} {url} returned {response.status_code} after "
                        f"{attempt} attempts: {response.text[:500]}"
                    )
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 400:
                raise ComfyUIAPIError(
                    f"ComfyUI {method} {url} returned {response.status_code}: {response.text[:500]}"
                )

            return response

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise ComfyUIAPIError(
                f"ComfyUI returned a non-JSON response ({response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
