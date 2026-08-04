"""ComfyUIVideoProvider / ComfyUIImageProvider unit tests, plus the
shared `_comfyui_common` client/template plumbing they're built on.
No real ComfyUI instance needed — `requests.request` is mocked, the same
methodology this codebase uses for every other HTTP-based provider
(RunwayProvider, ElevenLabsProvider): real submit/poll/fetch/error-
handling code, faked network. Also exercises the two shipped workflow
templates (config/comfyui/*.json) for real, since "do not hardcode
workflows" only means something if the templates actually load/render.
"""

from unittest import mock

import pytest

from libs.providers._comfyui_common import (
    ComfyUIAPIError,
    load_workflow_template,
    render_workflow,
)
from libs.providers.image_gen.comfyui_provider import ComfyUIImageProvider
from libs.providers.video_gen.comfyui_provider import ComfyUIVideoProvider


class _FakeResponse:
    def __init__(self, status_code, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


# --- workflow template loading/rendering ------------------------------------


@pytest.mark.parametrize(
    "path,expected_title",
    [("config/comfyui/text_to_image.json", "SaveImage"), ("config/comfyui/text_to_video.json", "VideoCombine")],
)
def test_shipped_templates_load_and_declare_output_node(path, expected_title):
    workflow, output_node_title = load_workflow_template(path)
    assert output_node_title == expected_title
    assert isinstance(workflow, dict) and workflow


@pytest.mark.parametrize(
    "path", ["config/comfyui/text_to_image.json", "config/comfyui/text_to_video.json"]
)
def test_shipped_templates_have_internally_consistent_wiring(path):
    """Every node-to-node reference must point at a node that exists.

    A ComfyUI graph wires nodes by id (`["10", 0]` = "output 0 of node
    10"), so a dangling id is only caught when ComfyUI itself rejects
    the submitted prompt — i.e. after a real generation call. Editing
    these templates (adding a hires pass, swapping an upscaler) is
    exactly when an id goes stale, so it is worth catching here instead.
    """
    workflow, _ = load_workflow_template(path)
    for node_id, node in workflow.items():
        for field, value in node["inputs"].items():
            is_reference = (
                isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            )
            if is_reference:
                assert value[0] in workflow, (
                    f"{path}: node {node_id}.{field} references missing node {value[0]!r}"
                )


def test_render_workflow_preserves_int_type_for_whole_value_placeholders():
    """ComfyUI validates node inputs by type — a width/seed field must
    come back as a real int, not a stringified one embedded in text.
    """
    workflow = {"5": {"inputs": {"width": "{{WIDTH}}", "text": "draw {{WIDTH}}px wide"}}}
    rendered = render_workflow(workflow, {"WIDTH": 1280})
    assert rendered["5"]["inputs"]["width"] == 1280
    assert isinstance(rendered["5"]["inputs"]["width"], int)
    assert rendered["5"]["inputs"]["text"] == "draw 1280px wide"


def test_render_workflow_does_not_mutate_the_original_template():
    workflow = {"6": {"inputs": {"text": "{{PROMPT}}"}}}
    render_workflow(workflow, {"PROMPT": "a cat"})
    assert workflow["6"]["inputs"]["text"] == "{{PROMPT}}"


def test_missing_template_file_raises_honestly():
    with pytest.raises(ComfyUIAPIError, match="not found"):
        load_workflow_template("config/comfyui/does_not_exist.json")


# --- ComfyUIImageProvider: full submit/poll/fetch flow ----------------------


def test_image_provider_full_flow_and_fills_placeholders():
    history_calls = {"n": 0}

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST" and url.endswith("/prompt"):
            assert json["prompt"]["6"]["inputs"]["text"] == "a red apple, studio lighting"
            # The base pass renders at the model's own comfortable
            # resolution; the hires pass below is what reaches output
            # size (see ComfyUIImageProvider's width/upscale_factor).
            assert json["prompt"]["5"]["inputs"]["width"] == 768
            assert json["prompt"]["5"]["inputs"]["height"] == 432
            # Every placeholder the shipped template declares must be
            # substituted — an unfilled one reaches ComfyUI as the
            # literal string "{{...}}" and is rejected as an invalid
            # node input, so this is the check that keeps the template
            # and the provider from silently drifting apart.
            base_sampler = json["prompt"]["3"]["inputs"]
            hires_sampler = json["prompt"]["11"]["inputs"]
            upscale = json["prompt"]["10"]["inputs"]
            assert upscale["width"] == 1536 and upscale["height"] == 864
            assert base_sampler["sampler_name"] == "dpmpp_2m"
            assert base_sampler["scheduler"] == "karras"
            assert isinstance(base_sampler["cfg"], float)
            assert isinstance(base_sampler["steps"], int)
            assert 0.0 < hires_sampler["denoise"] < 1.0
            for node in json["prompt"].values():
                for value in node["inputs"].values():
                    assert not (isinstance(value, str) and "{{" in value), (
                        f"unsubstituted placeholder reached ComfyUI: {value!r}"
                    )
            return _FakeResponse(200, {"prompt_id": "img1", "node_errors": {}})
        if "/history/img1" in url:
            history_calls["n"] += 1
            if history_calls["n"] < 2:
                return _FakeResponse(200, {})
            return _FakeResponse(
                200,
                {
                    "img1": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
                    }
                },
            )
        if url.endswith("/view"):
            assert params == {"filename": "a.png", "subfolder": "", "type": "output"}
            return _FakeResponse(200, content=b"REAL_PNG_BYTES")
        raise AssertionError(f"unexpected call: {method} {url}")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        result = provider.generate("a red apple, studio lighting")

    assert result == b"REAL_PNG_BYTES"
    assert history_calls["n"] == 2  # confirms polling actually happened, not a lucky first hit


def test_image_provider_honors_explicit_negative_prompt():
    """negative_prompt is a real generate() parameter, not just a
    template-baked default — a caller that passes one must see it reach
    ComfyUI's negative CLIPTextEncode node untouched.
    """

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST" and url.endswith("/prompt"):
            assert json["prompt"]["7"]["inputs"]["text"] == "extra fingers, bad anatomy"
            return _FakeResponse(200, {"prompt_id": "img2", "node_errors": {}})
        if "/history/img2" in url:
            return _FakeResponse(
                200,
                {"img2": {"status": {"status_str": "success"}, "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}},
            )
        if url.endswith("/view"):
            return _FakeResponse(200, content=b"BYTES")
        raise AssertionError(f"unexpected call: {method} {url}")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        provider.generate("a red apple", negative_prompt="extra fingers, bad anatomy")


def test_image_provider_defaults_negative_prompt_when_omitted():
    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST" and url.endswith("/prompt"):
            # Never an empty string reaching ComfyUI when the caller
            # (Asset Generation, Thumbnail Generation) passes nothing.
            assert json["prompt"]["7"]["inputs"]["text"]
            return _FakeResponse(200, {"prompt_id": "img3", "node_errors": {}})
        if "/history/img3" in url:
            return _FakeResponse(
                200,
                {"img3": {"status": {"status_str": "success"}, "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}},
            )
        if url.endswith("/view"):
            return _FakeResponse(200, content=b"BYTES")
        raise AssertionError(f"unexpected call: {method} {url}")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        provider.generate("a red apple")


def test_video_provider_finds_output_under_gifs_key():
    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST" and url.endswith("/prompt"):
            assert json["prompt"]["5"]["inputs"]["batch_size"] == 65
            return _FakeResponse(200, {"prompt_id": "vid1", "node_errors": {}})
        if "/history/vid1" in url:
            return _FakeResponse(
                200,
                {
                    "vid1": {
                        "status": {"status_str": "success"},
                        "outputs": {"11": {"gifs": [{"filename": "clip.mp4", "subfolder": "s", "type": "output"}]}},
                    }
                },
            )
        if url.endswith("/view"):
            return _FakeResponse(200, content=b"REAL_MP4_BYTES")
        raise AssertionError(f"unexpected call: {method} {url}")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIVideoProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        result = provider.generate("a spinning crankshaft")

    assert result == b"REAL_MP4_BYTES"


def test_node_errors_raise_comfyui_api_error_not_silently_ignored():
    def fake_request(method, url, *, json=None, params=None, timeout=None):
        return _FakeResponse(200, {"prompt_id": None, "node_errors": {"5": {"errors": ["bad width"]}}})

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={}, api_key=None)
        with pytest.raises(ComfyUIAPIError, match="node_errors"):
            provider.generate("test")


def test_job_error_status_raises_comfyui_api_error():
    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST":
            return _FakeResponse(200, {"prompt_id": "bad1"})
        if "/history/bad1" in url:
            return _FakeResponse(200, {"bad1": {"status": {"status_str": "error", "messages": ["OOM"]}}})
        raise AssertionError("should not reach /view")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        with pytest.raises(ComfyUIAPIError, match="failed"):
            provider.generate("test")


def test_unreachable_comfyui_raises_clear_error():
    import requests

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(
            config={"max_attempts": 1, "retry_backoff_sec": 0}, api_key=None
        )
        with pytest.raises(ComfyUIAPIError, match="is ComfyUI running"):
            provider.generate("test")


def test_no_matching_output_raises_honestly():
    """A job that completes but produces nothing under the expected
    media key (e.g. output_node_title pointed at the wrong node) must
    fail loudly, not return empty/garbage bytes.
    """

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if method == "POST":
            return _FakeResponse(200, {"prompt_id": "empty1"})
        if "/history/empty1" in url:
            return _FakeResponse(
                200, {"empty1": {"status": {"status_str": "success"}, "outputs": {"9": {}}}}
            )
        raise AssertionError("should not reach /view")

    with mock.patch("requests.request", side_effect=fake_request):
        provider = ComfyUIImageProvider(config={"poll_interval_sec": 0.01}, api_key=None)
        with pytest.raises(ComfyUIAPIError, match="produced no output"):
            provider.generate("test")
