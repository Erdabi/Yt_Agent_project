"""Ollama-backed `llm` provider — a local, self-hosted, free-inference
alternative to the cloud vendors above. Talks to a local Ollama server
(https://github.com/ollama/ollama, its documented REST API at
`/api/chat`) over plain HTTP, the same way `RunwayProvider`/
`ElevenLabsProvider` talk to their own vendor APIs via `requests` — no
extra SDK dependency beyond one already used elsewhere in this codebase.

Forcing a specific structured output does not use Ollama's separate
"tools"/function-calling mechanism (whether a given model actually calls
a specific tool on request is template/model-dependent and not
guaranteed the way Anthropic's `tool_choice` is). Instead this uses
Ollama's `format` request field: passing `tool.input_schema` — already a
plain JSON Schema dict, identical to what `AnthropicLLMProvider` hands
Anthropic as `input_schema` — makes Ollama grammar-constrain decoding to
that exact schema. That is a *stronger* forcing guarantee than
tool-calling gives (the output is schema-valid JSON by construction, not
merely "usually" schema-valid), and it needs no schema translation layer:
the same `LLMToolCall.input_schema` a caller already built for Anthropic
works here unchanged.
"""

import json
import time
from typing import Any

import requests

from libs.core.config import get_settings
from libs.core.logging import get_logger

from .base import LLMConfigError, LLMProvider, LLMResponseError, LLMToolCall, LLMToolResult

logger = get_logger(__name__)


class OllamaAPIError(RuntimeError):
    """Ollama rejected a request outright, or a request failed after
    exhausting its retry budget — distinct from a transient error (which
    this provider already retries internally). Raised honestly rather
    than swallowed, matching every other provider's error handling.
    """


class OllamaLLMProvider(LLMProvider):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        super().__init__(config=config, api_key=api_key)
        settings = get_settings()
        self._base_url = (config.get("base_url") or settings.ollama_url).rstrip("/")
        self._model = config.get("model") or settings.ollama_model
        self._temperature = float(config.get("temperature", 0.7))
        self._timeout_sec = float(config.get("timeout_sec", 180))
        self._max_attempts = int(config.get("max_attempts", 3))
        self._retry_backoff_sec = float(config.get("retry_backoff_sec", 2))
        #: Lazily probed via `/api/show` and cached — whether the
        #: configured model declares vision support, so `images` can be
        #: honestly dropped (with a warning) for a text-only local model
        #: like qwen3:32b instead of sending bytes it can't use.
        self._supports_vision: bool | None = None

    @property
    def model(self) -> str:
        return self._model

    def generate_tool_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool: LLMToolCall,
        images: list[bytes] | None = None,
        enable_web_research: bool = False,
    ) -> LLMToolResult:
        if enable_web_research:
            # No local equivalent of Anthropic's server-side web_search/
            # web_fetch tools exists in this environment (no local web
            # search tool was installed alongside Ollama/ComfyUI/Kokoro).
            # Best-effort per generate_tool_call's own contract: answer
            # from the model's own knowledge rather than refuse — the
            # caller (KnowledgeBuilder) is responsible for treating an
            # unverified package as such.
            logger.warning(
                "ollama_web_research_unavailable",
                model=self._model,
                detail="qwen3/Ollama has no server-side web search — answering from model knowledge only",
            )

        images = self._images_if_supported(images)
        user_message: dict[str, Any] = {"role": "user", "content": user_prompt}
        if images:
            import base64

            user_message["images"] = [base64.b64encode(b).decode("ascii") for b in images]

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                user_message,
            ],
            "format": tool.input_schema,
            "stream": False,
            "options": {"temperature": self._temperature},
        }

        response = self._request_with_retry("POST", f"{self._base_url}/api/chat", json=body)
        data = self._json(response)

        message = data.get("message") or {}
        content = message.get("content")
        if not content:
            raise LLMResponseError(
                f"Ollama returned no content for model {self._model!r} (done_reason="
                f"{data.get('done_reason')!r})"
            )
        try:
            tool_input = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise LLMResponseError(
                f"Ollama's response for {tool.name!r} was not valid JSON: {content[:500]!r}"
            ) from exc

        return LLMToolResult(
            tool_input=tool_input,
            model=self._model,
            provider_name="OllamaLLMProvider",
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
        )

    # --- vision capability probing ------------------------------------------

    def _images_if_supported(self, images: list[bytes] | None) -> list[bytes] | None:
        if not images:
            return None
        if self._supports_vision is None:
            self._supports_vision = self._probe_vision_support()
        if self._supports_vision:
            return images
        logger.warning(
            "ollama_model_lacks_vision",
            model=self._model,
            detail="dropping images and continuing text-only — configure a vision-capable "
            "Ollama model to review actual image content",
        )
        return None

    def _probe_vision_support(self) -> bool:
        try:
            response = requests.post(
                f"{self._base_url}/api/show",
                json={"model": self._model},
                timeout=self._timeout_sec,
            )
            if response.status_code >= 400:
                return False
            data = response.json()
        except (requests.exceptions.RequestException, ValueError):
            # Ollama unreachable/misbehaving — the actual chat call below
            # will raise a clear error of its own; no need to double-report
            # here, just conservatively assume no vision support.
            return False
        return "vision" in (data.get("capabilities") or [])

    # --- HTTP plumbing -------------------------------------------------------

    def _request_with_retry(self, method: str, url: str, *, json: dict) -> requests.Response:
        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            try:
                response = requests.request(method, url, json=json, timeout=self._timeout_sec)
            except requests.exceptions.RequestException as exc:
                if is_last_attempt:
                    raise LLMResponseError(
                        f"Ollama {method} {url} failed after {attempt} attempts: {exc} — is "
                        f"Ollama running at {self._base_url!r}?"
                    ) from exc
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code == 404 and "model" in response.text.lower():
                # Ollama's own honest signal that the model isn't pulled —
                # retrying an identical request won't fix that.
                raise LLMConfigError(
                    f"Ollama has no model {self._model!r} available — run "
                    f"'ollama pull {self._model}' before selecting it as the active llm provider "
                    f"(response: {response.text[:300]})"
                )

            if response.status_code >= 500:
                if is_last_attempt:
                    raise LLMResponseError(
                        f"Ollama {method} {url} returned {response.status_code} after "
                        f"{attempt} attempts: {response.text[:500]}"
                    )
                time.sleep(self._retry_backoff_sec * attempt)
                continue

            if response.status_code >= 400:
                raise LLMResponseError(
                    f"Ollama {method} {url} returned {response.status_code}: {response.text[:500]}"
                )

            return response

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise LLMResponseError(
                f"Ollama returned a non-JSON response ({response.status_code}): "
                f"{response.text[:500]}"
            ) from exc
