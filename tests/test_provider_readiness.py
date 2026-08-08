"""Provider readiness — "can this capability actually serve a request
right now?" — and the CLI decision that depends on it.

This exists because of a real, expensive failure. `scripts/run_pipeline.py`
turns "which capabilities work" into a hard constraint in the Script
Agent's prompt, and it used to answer that question by checking whether
the configured provider class name started with `Stub`. A real
`ComfyUIVideoProvider` pointed at a ComfyUI instance without the
AnimateDiff custom nodes its workflow template needs passes that check
and then fails *every* request with HTTP 400 `missing_node_type`. The
Script Agent was accordingly told `ai_video`/`animation` were available,
wrote all eight segments around `animation`, and the run died at
video_creation with a complete script already generated and paid for.

So the tests here pin the distinction that failure turned on: a provider
being *implemented* is not the same as its backend being *able*, and only
the provider itself can tell the two apart.
"""

from unittest import mock

import pytest

from libs.providers._comfyui_common import ComfyUIClient, workflow_node_types
from libs.providers.base import Provider, ProviderReadiness, StubProvider
from libs.providers.image_gen.comfyui_provider import ComfyUIImageProvider
from libs.providers.image_gen.stub_provider import StubImageGenProvider
from libs.providers.video_gen.comfyui_provider import ComfyUIVideoProvider
from libs.providers.video_gen.stub_provider import StubVideoGenProvider


class _FakeResponse:
    def __init__(self, status_code, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        return self._json_data


def _object_info_responder(installed_node_types):
    """Fakes ComfyUI's `/object_info`, whose top-level keys are exactly
    the node classes the instance has loaded.
    """

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        if url.endswith("/object_info"):
            return _FakeResponse(200, {name: {} for name in installed_node_types})
        raise AssertionError(f"unexpected call: {method} {url}")

    return fake_request


# --- the graph-inspection primitive -----------------------------------------


def test_workflow_node_types_collects_every_referenced_class():
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {}},
        "2": {"class_type": "VAEDecode", "inputs": {}},
        "3": {"class_type": "KSampler", "inputs": {}},
    }
    assert workflow_node_types(workflow) == {"KSampler", "VAEDecode"}


def test_workflow_node_types_ignores_malformed_entries():
    """A template is user-supplied JSON, so it can contain anything; a
    node without a string `class_type` must not crash a readiness check
    whose whole job is to answer instead of raise.
    """
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {}},
        "2": {"inputs": {}},
        "3": "not a node",
        "4": {"class_type": 17},
    }
    assert workflow_node_types(workflow) == {"KSampler"}


def test_missing_node_types_is_the_difference_against_what_is_installed():
    client = ComfyUIClient(base_url="http://comfy.test", max_attempts=1, retry_backoff_sec=0)
    workflow = {
        "1": {"class_type": "KSampler", "inputs": {}},
        "2": {"class_type": "ADE_AnimateDiffLoaderWithContext", "inputs": {}},
    }
    with mock.patch("requests.request", side_effect=_object_info_responder(["KSampler", "VAEDecode"])):
        assert client.missing_node_types(workflow) == {"ADE_AnimateDiffLoaderWithContext"}


# --- provider readiness ------------------------------------------------------


def test_comfyui_image_provider_is_ready_when_every_node_is_installed():
    provider = ComfyUIImageProvider(config={}, api_key=None)
    installed = workflow_node_types(provider._workflow)
    with mock.patch("requests.request", side_effect=_object_info_responder(installed)):
        readiness = provider.check_readiness()
    assert readiness.ready
    assert readiness.reason == ""


def test_comfyui_video_provider_is_unready_when_custom_nodes_are_missing():
    """The exact production failure: a real provider, a reachable
    ComfyUI, and a workflow the instance cannot run.
    """
    provider = ComfyUIVideoProvider(config={}, api_key=None)
    installed = workflow_node_types(provider._workflow) - {"ADE_AnimateDiffLoaderWithContext"}
    with mock.patch("requests.request", side_effect=_object_info_responder(installed)):
        readiness = provider.check_readiness()

    assert not readiness.ready
    # The reason has to name the missing class — "video_gen is
    # unavailable" alone leaves an operator with nothing to act on.
    assert "ADE_AnimateDiffLoaderWithContext" in readiness.reason
    assert "video_gen" in readiness.reason


def test_unreachable_comfyui_reports_unready_rather_than_raising():
    """`check_readiness()` is a question, not an operation: a backend
    that cannot be reached is a legitimate "no", and callers deciding
    which asset types to allow must not have to wrap it in try/except.
    """
    import requests

    def fake_request(method, url, *, json=None, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("refused")

    provider = ComfyUIImageProvider(
        config={"max_attempts": 1, "retry_backoff_sec": 0}, api_key=None
    )
    with mock.patch("requests.request", side_effect=fake_request):
        readiness = provider.check_readiness()

    assert not readiness.ready
    assert "not reachable" in readiness.reason


@pytest.mark.parametrize("stub_class", [StubVideoGenProvider, StubImageGenProvider])
def test_stubs_report_unready_with_their_own_explanation(stub_class):
    stub = stub_class(config={}, api_key=None)
    readiness = stub.check_readiness()
    assert not readiness.ready
    # One message, two exits: what a caller is told ahead of time and
    # what a direct call raises must be the same text, or the two drift.
    assert readiness.reason == stub.unavailable_reason
    with pytest.raises(NotImplementedError, match="No real"):
        stub.generate("anything")


def test_providers_are_ready_by_default():
    """Implementing `check_readiness()` must stay optional — a provider
    with no cheap way to probe its backend should answer optimistically
    and fail honestly at call time, not be forced to invent a check.
    """

    class _NoCheckProvider(Provider):
        pass

    assert _NoCheckProvider(config={}, api_key=None).check_readiness().ready


def test_stub_provider_subclasses_carry_a_real_reason():
    """Guards the shared base's placeholder text from shipping as a
    subclass's actual explanation.
    """
    for stub_class in (StubVideoGenProvider, StubImageGenProvider):
        assert stub_class.unavailable_reason != StubProvider.unavailable_reason
        assert len(stub_class.unavailable_reason) > 40


# --- the CLI decision that depends on all of the above -----------------------


def _readiness_map(**overrides):
    base = {
        "image_gen": ProviderReadiness.ok(),
        "video_gen": ProviderReadiness.ok(),
        "stock_media": ProviderReadiness.ok(),
        "audio_library": ProviderReadiness.ok(),
    }
    base.update(overrides)
    return base


def test_unready_capability_removes_its_asset_types_from_the_allowed_list():
    """The regression itself. `video_gen` being unavailable must drop
    *both* asset types that route to it — a script allowed to request
    `animation` fails exactly as hard as one allowed `ai_video`.
    """
    from scripts.run_pipeline import _supported_asset_types

    supported = _supported_asset_types(
        _readiness_map(video_gen=ProviderReadiness.unavailable("missing custom nodes"))
    )

    assert "ai_video" not in supported
    assert "animation" not in supported
    # Unrelated capabilities are untouched — this narrows precisely.
    assert "ai_image" in supported and "portrait" in supported
    assert "stock_footage" in supported


def test_render_time_asset_types_survive_every_capability_being_unavailable():
    """`text_overlay` and `subtitle_emphasis` are never routed to a
    provider, so no provider outage can take them away — without them a
    script would have no legal visual requirement at all.
    """
    from scripts.run_pipeline import _supported_asset_types

    all_down = {
        capability: ProviderReadiness.unavailable("down")
        for capability in ("image_gen", "video_gen", "stock_media", "audio_library")
    }
    assert set(_supported_asset_types(all_down)) == {"text_overlay", "subtitle_emphasis"}


def test_constraint_note_forbids_exactly_what_is_unavailable():
    """The note is the Script Agent's only view of these limits, so the
    forbidden list must be derived from the same readiness answer as the
    allowed list — never a separately maintained list of names that can
    contradict it.
    """
    from scripts.run_pipeline import build_production_constraint_note

    note = build_production_constraint_note(
        _readiness_map(video_gen=ProviderReadiness.unavailable("missing custom nodes"))
    )

    allowed, _, forbidden = note.partition("Never propose")
    assert "ai_video" in forbidden and "animation" in forbidden
    assert "ai_video" not in allowed and "animation" not in allowed
    assert "ai_image" in allowed


def test_constraint_note_omits_the_prohibition_when_nothing_is_unavailable():
    """A note ending in "Never propose ." would be both sloppy and
    confusing; with every capability up there is nothing to forbid.
    """
    from scripts.run_pipeline import build_production_constraint_note

    note = build_production_constraint_note(_readiness_map())
    assert "Never propose" not in note
    for asset_type in ("ai_video", "animation", "ai_image", "stock_footage"):
        assert asset_type in note


# --- the note has to reach the channel a re-run actually uses ---------------


def test_reused_channel_gets_a_refreshed_constraint_note(require_postgres):
    """A capability fix is worthless if channels keep the snapshot taken
    before it. This is the second half of the same production failure:
    the Rome channel was set up while `video_gen` was wrongly believed
    available, so every later run on that channel would have kept
    requesting `animation` no matter how correct the detection became.
    """
    import uuid

    from libs.core.db import sync_session_scope
    from libs.models import Channel
    from scripts.run_pipeline import get_or_create_channel

    name = f"readiness-test-{uuid.uuid4().hex[:12]}"
    stale_note = "PRODUCTION CONSTRAINT ...: ai_video, animation, ai_image."
    fresh_note = "PRODUCTION CONSTRAINT ...: ai_image. Never propose ai_video, animation."

    channel_id, created = get_or_create_channel(
        name=name,
        niche="history",
        operator_persona="Warm, factual narrator.",
        production_note=stale_note,
        banned_topics=[],
    )
    assert created

    reused_id, created_again = get_or_create_channel(
        name=name,
        niche="history",
        operator_persona=None,
        production_note=fresh_note,
        banned_topics=[],
    )
    assert reused_id == channel_id and not created_again

    try:
        with sync_session_scope() as session:
            persona_config = session.get(Channel, uuid.UUID(channel_id)).persona_config
    finally:
        with sync_session_scope() as session:
            session.delete(session.get(Channel, uuid.UUID(channel_id)))

    persona = persona_config["persona"]
    assert fresh_note in persona
    assert stale_note not in persona
    # The operator's own words survive a refresh they didn't ask for,
    # even though this run passed no --persona at all.
    assert "Warm, factual narrator." in persona
    assert persona_config["operator_persona"] == "Warm, factual narrator."


def test_every_asset_capability_is_one_the_registry_can_resolve():
    """`ASSET_TYPE_CAPABILITY` names capabilities that must exist in
    config/providers.yaml. A capability renamed on one side only would
    silently mark asset types unsupported forever, which is invisible —
    the run just quietly produces a narrower script.
    """
    from libs.core.config import get_settings
    from libs.providers.registry import _load_config
    from libs.schemas.script_production import ASSET_TYPE_CAPABILITY

    configured = set(_load_config(get_settings().providers_config_path))
    named = {capability for capability in ASSET_TYPE_CAPABILITY.values() if capability}
    assert named <= configured
