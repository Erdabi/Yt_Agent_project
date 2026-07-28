"""Provider Registry resolution + OllamaLLMProvider unit tests.

No Postgres/Redis needed — these exercise `libs.providers.get_provider`
and the concrete provider classes directly, mocking only the HTTP layer
(the same methodology this codebase's own manual verification scripts
use for every other provider: real provider/parsing/retry code, faked
network). Confirms two things the local-provider migration must hold:
the registry resolves the new local providers by default, and swapping
back to a cloud provider (e.g. `anthropic`) still works via the
`{CAPABILITY}_PROVIDER` env var without editing config/providers.yaml.
"""

import json
from unittest import mock

import pytest
import yaml

from libs.providers.llm.anthropic_provider import AnthropicLLMProvider
from libs.providers.llm.base import LLMConfigError, LLMResponseError, LLMToolCall
from libs.providers.llm.ollama_provider import OllamaLLMProvider
from libs.providers.registry import get_provider


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> dict:
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data


_TOOL = LLMToolCall(
    name="propose_video_ideas",
    description="test tool",
    input_schema={
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
        "additionalProperties": False,
    },
)


def test_config_declares_local_providers_active():
    """The actual shipped config/providers.yaml (not a fixture) defaults
    every capability this task touches to a local, no-API-key provider.
    """
    with open("config/providers.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    assert config["llm"]["active"] == "ollama"
    assert config["tts"]["active"] == "kokoro"
    assert config["video_gen"]["active"] == "comfyui"
    assert config["image_gen"]["active"] == "comfyui"


def test_registry_resolves_ollama_by_default():
    provider = get_provider("llm")
    assert isinstance(provider, OllamaLLMProvider)


def test_registry_resolves_kokoro_by_default():
    from libs.providers.tts.kokoro_provider import KokoroTTSProvider

    provider = get_provider("tts")
    assert isinstance(provider, KokoroTTSProvider)


def test_registry_resolves_comfyui_by_default_for_video_and_image():
    from libs.providers.image_gen.comfyui_provider import ComfyUIImageProvider
    from libs.providers.video_gen.comfyui_provider import ComfyUIVideoProvider

    assert isinstance(get_provider("video_gen"), ComfyUIVideoProvider)
    assert isinstance(get_provider("image_gen"), ComfyUIImageProvider)


def test_env_override_still_swaps_to_anthropic(monkeypatch):
    """The `{CAPABILITY}_PROVIDER` env var (libs/providers/registry.py)
    still takes precedence over config/providers.yaml's `active` line —
    confirms the local-first default didn't break provider swapping.
    """
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    provider = get_provider("llm")
    assert isinstance(provider, AnthropicLLMProvider)


def test_ollama_generate_tool_call_forces_structured_json_output():
    """Ollama has no `tool_choice`-style forcing — this provider must use
    the `format` (JSON-schema-constrained decoding) request field instead,
    passing the *same* `LLMToolCall.input_schema` a caller already built
    for Anthropic, unmodified.
    """
    captured = {}

    def fake_post(method, url, *, json=None, timeout=None):
        assert method == "POST"
        assert url.endswith("/api/chat")
        captured["body"] = json
        assert json["format"] == _TOOL.input_schema
        return _FakeResponse(
            200,
            {
                "message": {"role": "assistant", "content": '{"topic": "how engines work"}'},
                "prompt_eval_count": 42,
                "eval_count": 17,
                "done_reason": "stop",
            },
        )

    with mock.patch("requests.request", side_effect=fake_post):
        provider = OllamaLLMProvider(config={"model": "qwen3:32b"}, api_key=None)
        result = provider.generate_tool_call(
            system_prompt="sys", user_prompt="user", tool=_TOOL,
        )

    assert result.tool_input == {"topic": "how engines work"}
    assert result.model == "qwen3:32b"
    assert result.provider_name == "OllamaLLMProvider"
    assert result.input_tokens == 42
    assert result.output_tokens == 17
    assert result.used_web_research is False
    assert captured["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_ollama_enable_web_research_degrades_gracefully_instead_of_raising():
    """qwen3/Ollama has no server-side web search — `enable_web_research`
    must not be treated as a hard requirement (see generate_tool_call's
    docstring in libs/providers/llm/base.py): the call still succeeds,
    just without `used_web_research`.
    """

    def fake_post(method, url, *, json=None, timeout=None):
        return _FakeResponse(
            200,
            {
                "message": {"role": "assistant", "content": '{"topic": "engines"}'},
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        )

    with mock.patch("requests.request", side_effect=fake_post):
        provider = OllamaLLMProvider(config={}, api_key=None)
        result = provider.generate_tool_call(
            system_prompt="sys", user_prompt="user", tool=_TOOL, enable_web_research=True,
        )
    assert result.tool_input == {"topic": "engines"}
    assert result.used_web_research is False


def test_ollama_missing_model_raises_config_error():
    def fake_post(method, url, *, json=None, timeout=None):
        return _FakeResponse(404, text='{"error":"model \\"qwen3:32b\\" not found"}')

    with mock.patch("requests.request", side_effect=fake_post):
        provider = OllamaLLMProvider(
            config={"max_attempts": 1, "retry_backoff_sec": 0}, api_key=None
        )
        with pytest.raises(LLMConfigError, match="ollama pull"):
            provider.generate_tool_call(system_prompt="sys", user_prompt="user", tool=_TOOL)


def test_ollama_unreachable_raises_response_error():
    import requests

    def fake_post(method, url, *, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("connection refused")

    with mock.patch("requests.request", side_effect=fake_post):
        provider = OllamaLLMProvider(
            config={"max_attempts": 1, "retry_backoff_sec": 0}, api_key=None
        )
        with pytest.raises(LLMResponseError, match="is Ollama running"):
            provider.generate_tool_call(system_prompt="sys", user_prompt="user", tool=_TOOL)


def test_ollama_drops_images_for_non_vision_model_instead_of_failing():
    """qwen3:32b is text-only. Passing `images=` must not blow up the
    whole QA/Thumbnail review — the provider drops them and answers
    text-only (see generate_tool_call's docstring on graceful
    degradation), the same contract TTSProvider/word_timings already
    establishes for a capability a given local model lacks.
    """
    calls = []

    def fake_post(method, url, *, json=None, timeout=None):
        if url.endswith("/api/show"):
            return _FakeResponse(200, {"capabilities": ["completion"]})
        calls.append(json)
        return _FakeResponse(
            200,
            {"message": {"content": '{"topic": "x"}'}, "prompt_eval_count": 1, "eval_count": 1},
        )

    with mock.patch("requests.request", side_effect=fake_post):
        provider = OllamaLLMProvider(config={}, api_key=None)
        result = provider.generate_tool_call(
            system_prompt="sys", user_prompt="user", tool=_TOOL, images=[b"fake-png-bytes"],
        )
    assert result.tool_input == {"topic": "x"}
    assert "images" not in calls[0]["messages"][1]
