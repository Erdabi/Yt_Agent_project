"""A script may only ask for what this deployment can actually produce.

The permitted asset types were already stated in the Script Agent's
prompt — and nothing checked the resulting script against that sentence.
A model that overlooks one line of a long prompt produces a script that
passes every structural validation and is still physically impossible.

That is not theoretical. One run reached `needs_human_review` after 4.6
hours: the script put a single `background_music_cue` on its final beat,
no audio_library provider exists here, and the contradiction only
surfaced inside Asset Generation — after 10 narration files and 28 images
had already been generated, four times over across retries.

So the tests here pin the rule that a capability limit stated only in a
prompt is a suggestion, and that enforcement reads the live provider
registry rather than any hardcoded list.
"""

from unittest import mock

import pytest

from libs.providers.base import ProviderReadiness
from libs.providers.capabilities import (
    fulfillable_asset_types,
    unfulfillable_reasons,
)
from libs.schemas.script_production import (
    ASSET_TYPE_CAPABILITY,
    VISUAL_ASSET_TYPES,
    AssetType,
)


def _readiness(**overrides):
    base = {
        "image_gen": ProviderReadiness.ok(),
        "video_gen": ProviderReadiness.ok(),
        "stock_media": ProviderReadiness.ok(),
        "audio_library": ProviderReadiness.ok(),
    }
    base.update(overrides)
    return base


# --- the shared map ---------------------------------------------------------


def test_every_asset_type_declares_what_produces_it():
    """A new AssetType added without a capability entry would silently
    become unfulfillable everywhere, which is invisible — scripts just
    quietly stop being allowed to use it.
    """
    assert set(ASSET_TYPE_CAPABILITY) == set(AssetType)


def test_only_the_render_time_types_need_no_provider():
    render_time = {
        asset_type
        for asset_type, capability in ASSET_TYPE_CAPABILITY.items()
        if capability is None
    }
    assert render_time == {AssetType.TEXT_OVERLAY, AssetType.SUBTITLE_EMPHASIS}


def test_the_video_agents_routing_agrees_with_the_shared_map():
    """services/agent_video keeps its own richer routing table (it also
    records how each type is resolved). The capability half of it must
    match the shared map, or the Script Agent would enforce one rule
    while the Video Agent followed another.
    """
    from services.agent_video.app.pipeline_schema import ASSET_TYPE_ROUTING

    for asset_type, routing in ASSET_TYPE_ROUTING.items():
        capability = routing[0]
        if capability is None:
            continue
        assert ASSET_TYPE_CAPABILITY[asset_type] == capability, asset_type


# --- fulfillability ---------------------------------------------------------


def test_an_unavailable_capability_removes_exactly_its_own_asset_types():
    allowed = fulfillable_asset_types(
        _readiness(audio_library=ProviderReadiness.unavailable("no provider"))
    )
    assert AssetType.SOUND_EFFECT not in allowed
    assert AssetType.BACKGROUND_MUSIC_CUE not in allowed
    assert AssetType.AI_IMAGE in allowed and AssetType.ANIMATION in allowed


def test_render_time_types_survive_every_provider_being_down():
    """Without them a script would have no legal visual requirement at
    all, and `_validate_asset_coverage` would make every script invalid.
    """
    all_down = {
        capability: ProviderReadiness.unavailable("down")
        for capability in ("image_gen", "video_gen", "stock_media", "audio_library")
    }
    allowed = fulfillable_asset_types(all_down)
    assert allowed == {AssetType.TEXT_OVERLAY, AssetType.SUBTITLE_EMPHASIS}
    assert allowed & VISUAL_ASSET_TYPES


def test_reasons_name_what_is_actually_missing():
    """"background_music_cue is unavailable" leaves an operator with
    nothing to do; the provider's own explanation tells them what to
    install.
    """
    reasons = unfulfillable_reasons(
        _readiness(audio_library=ProviderReadiness.unavailable("no audio library configured"))
    )
    assert reasons[AssetType.SOUND_EFFECT] == "no audio library configured"
    assert AssetType.AI_IMAGE not in reasons


# --- enforcement in the Script Agent ----------------------------------------


def _beat(text: str, *asset_types: str) -> dict:
    return {
        "voiceover_text": text,
        "scene_description": f"scene for {text}",
        "visual_suggestions": ["a relevant visual"],
        "production_metadata": {
            "camera_framing": "wide establishing shot",
            "asset_requirements": [
                {"asset_type": asset_type, "description": f"asset for {text}"}
                for asset_type in asset_types
            ],
            "transition_type": "cut",
            "pacing": "medium",
            "narration_emotion": "measured",
            "emphasis_words": [],
            "estimated_speech_wpm": 150,
        },
    }


def _script(*, cta_asset_type: str) -> dict:
    """Mirrors the shape of the script that actually failed in
    production: the offending beat pairs the unfulfillable type with a
    `text_overlay`, so it satisfies `_validate_asset_coverage` (which
    only requires *a* visual) and reaches Asset Generation looking
    perfectly well-formed.
    """
    return {
        "structure_notes": "chronological",
        "retention_notes": "open loops",
        "hook": _beat("a striking opening question", "ai_image"),
        "introduction": _beat("what this video will cover", "ai_image"),
        "main_sections": [
            {**_beat("the founding years", "ai_image"), "heading": "Part One"},
            {**_beat("the years of expansion", "ai_image"), "heading": "Part Two"},
        ],
        "ending": _beat("what Rome left behind", "ai_image"),
        "call_to_action": _beat("subscribe for more history", cta_asset_type, "text_overlay"),
    }


def _validate(payload, readiness):
    """Runs the validator exactly as `generate()` does — resolving the
    permitted set and the reasons once, then passing both in.
    """
    from services.agent_scriptwriter.app.script_generator import (
        _validate_asset_types_are_fulfillable,
    )
    from services.agent_scriptwriter.app.script_schema import script_from_dict

    _validate_asset_types_are_fulfillable(
        script_from_dict(payload),
        fulfillable_asset_types(readiness),
        unfulfillable_reasons(readiness),
    )


def test_a_script_asking_for_an_unavailable_type_is_rejected():
    """The exact production failure: one background_music_cue on the
    final beat, with no audio_library provider.
    """
    with pytest.raises(ValueError) as excinfo:
        _validate(
            _script(cta_asset_type="background_music_cue"),
            _readiness(audio_library=ProviderReadiness.unavailable("none configured")),
        )

    message = str(excinfo.value)
    # Names the offending beat, the reason, and the legal alternatives —
    # everything the repair call needs to fix it in one round.
    assert "call_to_action" in message
    assert "background_music_cue" in message
    assert "none configured" in message
    assert "ai_image" in message


def test_a_script_within_the_available_types_passes():
    _validate(
        _script(cta_asset_type="ai_image"),
        _readiness(audio_library=ProviderReadiness.unavailable("none configured")),
    )


def test_the_same_script_is_accepted_once_the_capability_is_configured():
    """Enforcement must track the environment, not a hardcoded list —
    configuring an audio library makes background_music_cue legal again
    with no code change.
    """
    _validate(_script(cta_asset_type="background_music_cue"), _readiness())


def test_generate_actually_runs_the_check_and_repairs_the_script():
    """The one that matters: the validator existing is worthless if
    `generate()` does not call it. The original bug *was* an unenforced
    rule, so a regression that drops the call has to fail a test — and
    checking the validator in isolation would not notice.

    Also proves the rejection reaches the repair loop, so an
    unfulfillable script costs one extra model round rather than the
    whole video_creation stage.
    """
    from services.agent_scriptwriter.app.script_generator import ScriptGenerator

    bad = _script(cta_asset_type="background_music_cue")
    good = _script(cta_asset_type="ai_image")

    generator = ScriptGenerator()
    generator._prompts = mock.Mock()
    template = mock.Mock(version="v1")
    template.render.return_value = "rendered"
    generator._prompts.get.return_value = template

    provider = mock.Mock(model="test-model")
    provider.generate_tool_call.side_effect = [
        mock.Mock(tool_input=payload, input_tokens=1, output_tokens=1)
        for payload in (bad, good)
    ]

    context = mock.Mock()
    context.prompt_version = "latest"
    context.project.project_id = "44444444-4444-4444-4444-444444444444"
    context.knowledge_package = None

    down = _readiness(audio_library=ProviderReadiness.unavailable("none configured"))
    with mock.patch(
        "services.agent_scriptwriter.app.script_generator.get_provider",
        return_value=provider,
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.get_settings",
        return_value=mock.Mock(script_repair_attempts=2),
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.track_llm_call",
        return_value=mock.MagicMock(),
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.fulfillable_asset_types",
        return_value=fulfillable_asset_types(down),
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.unfulfillable_reasons",
        return_value=unfulfillable_reasons(down),
    ):
        script = generator.generate(context)

    types = {
        requirement.asset_type
        for requirement in script.call_to_action.production.asset_requirements
    }
    assert AssetType.BACKGROUND_MUSIC_CUE not in types
    assert provider.generate_tool_call.call_count == 2, "the rejection never reached repair"


def test_the_permitted_set_is_resolved_once_not_per_repair_round():
    """Re-probing per round would let the answer change midway through
    fixing one script, and would put an HTTP call in a retry loop.
    """
    from services.agent_scriptwriter.app.script_generator import ScriptGenerator

    bad = _script(cta_asset_type="background_music_cue")
    good = _script(cta_asset_type="ai_image")

    generator = ScriptGenerator()
    generator._prompts = mock.Mock()
    template = mock.Mock(version="v1")
    template.render.return_value = "rendered"
    generator._prompts.get.return_value = template

    provider = mock.Mock(model="test-model")
    provider.generate_tool_call.side_effect = [
        mock.Mock(tool_input=payload, input_tokens=1, output_tokens=1)
        for payload in (bad, good)
    ]

    context = mock.Mock()
    context.prompt_version = "latest"
    context.project.project_id = "55555555-5555-5555-5555-555555555555"
    context.knowledge_package = None

    down = _readiness(audio_library=ProviderReadiness.unavailable("none configured"))
    resolver = mock.Mock(return_value=fulfillable_asset_types(down))
    with mock.patch(
        "services.agent_scriptwriter.app.script_generator.get_provider",
        return_value=provider,
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.get_settings",
        return_value=mock.Mock(script_repair_attempts=2),
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.track_llm_call",
        return_value=mock.MagicMock(),
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.fulfillable_asset_types",
        resolver,
    ), mock.patch(
        "services.agent_scriptwriter.app.script_generator.unfulfillable_reasons",
        return_value=unfulfillable_reasons(down),
    ):
        generator.generate(context)

    assert resolver.call_count == 1


def test_the_validator_makes_no_provider_calls():
    """It runs once per repair round, so it must stay pure — a network
    probe here would put an HTTP call inside a validation and let the
    answer change between rounds of fixing the same script.
    """
    with mock.patch(
        "libs.providers.capabilities.get_provider",
        side_effect=AssertionError("the validator resolved providers itself"),
    ):
        _validate(_script(cta_asset_type="ai_image"), _readiness())
