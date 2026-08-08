"""The Script Agent's validation-repair loop.

`script_from_dict()` rejects a script with a deterministic, precise
message — "main_sections[7] repeats main_sections[5] verbatim". Before
this loop existed, that message was logged and thrown away: the job
failed, the Manager retried, and the Script Agent re-issued the *identical*
prompt, hoping a fresh sample would happen not to repeat a beat. Two
consecutive production runs failed that way on the same validation.

The loop hands the rejected script and its rejection reason back to the
model instead. What these tests pin is that it is a repair and not a
loophole: the same validation runs on every pass, so nothing invalid can
be talked into being accepted, and the attempt budget is finite.
"""

import copy
from unittest import mock

import pytest

from libs.schemas.script_production import AssetType
from services.agent_scriptwriter.app.script_generator import (
    ScriptGenerationError,
    ScriptGenerator,
)


def _beat(text: str) -> dict:
    return {
        "voiceover_text": text,
        "scene_description": f"scene for {text}",
        "visual_suggestions": ["a relevant visual"],
        "production_metadata": {
            "camera_framing": "wide establishing shot",
            "asset_requirements": [
                {"asset_type": "ai_image", "description": f"an image of {text}"}
            ],
            "transition_type": "cut",
            "pacing": "medium",
            "narration_emotion": "measured",
            "emphasis_words": [],
            "estimated_speech_wpm": 150,
        },
    }


def _script_payload(*, ending_text: str) -> dict:
    return {
        "structure_notes": "chronological",
        "retention_notes": "open loops",
        "hook": _beat("a striking opening question"),
        "introduction": _beat("what this video will cover"),
        "main_sections": [
            {**_beat("the founding years"), "heading": "Part One"},
            {**_beat("the years of expansion"), "heading": "Part Two"},
        ],
        "ending": _beat(ending_text),
        "call_to_action": _beat("subscribe for more history"),
    }


@pytest.fixture
def generator_with_responses():
    """A ScriptGenerator whose provider returns `payloads` in order, and
    whose prompt loader is stubbed so these tests exercise the loop
    rather than the template files.
    """

    def _build(payloads, *, repair_attempts=2):
        generator = ScriptGenerator()
        generator._prompts = mock.Mock()
        template = mock.Mock(version="v1")
        template.render.return_value = "rendered"
        generator._prompts.get.return_value = template

        provider = mock.Mock(model="test-model")
        provider.generate_tool_call.side_effect = [
            mock.Mock(tool_input=p, input_tokens=1, output_tokens=1) for p in payloads
        ]

        context = mock.Mock()
        context.prompt_version = "latest"
        context.project.project_id = "33333333-3333-3333-3333-333333333333"
        # Rendered into the generation prompt when present; these tests
        # are about the repair loop, not knowledge-package formatting.
        context.knowledge_package = None

        settings = mock.Mock(script_repair_attempts=repair_attempts)
        patches = (
            mock.patch(
                "services.agent_scriptwriter.app.script_generator.get_provider",
                return_value=provider,
            ),
            mock.patch(
                "services.agent_scriptwriter.app.script_generator.get_settings",
                return_value=settings,
            ),
            mock.patch(
                "services.agent_scriptwriter.app.script_generator.track_llm_call",
                return_value=mock.MagicMock(),
            ),
            # `generate()` asks the provider registry which asset types
            # are fulfillable, and the ComfyUI providers answer that with
            # a real HTTP call to /object_info. Left unpatched, every
            # test in this file would depend on a ComfyUI being up — it
            # would pass on a developer machine running one and fail in
            # CI, or on the same machine an hour later. These tests are
            # about the repair loop; fulfillability has its own file
            # (test_asset_type_fulfillability.py), so the answer is
            # pinned to "everything is available" here.
            mock.patch(
                "services.agent_scriptwriter.app.script_generator.fulfillable_asset_types",
                return_value=set(AssetType),
            ),
            mock.patch(
                "services.agent_scriptwriter.app.script_generator.unfulfillable_reasons",
                return_value={},
            ),
        )
        return generator, provider, context, patches

    return _build


def _run(generator, context, patches):
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        return generator.generate(context)


def test_a_rejected_script_is_repaired_rather_than_failed(generator_with_responses):
    """The regression: a duplicate-beat script that the model then fixes
    must produce a valid script, not a failed job.
    """
    bad = _script_payload(ending_text="what this video will cover")  # dupes introduction
    good = _script_payload(ending_text="what Rome left behind")

    generator, provider, context, patches = generator_with_responses([bad, good])
    script = _run(generator, context, patches)

    assert script.ending.voiceover_text == "what Rome left behind"
    # One generate + one repair.
    assert provider.generate_tool_call.call_count == 2


def test_the_repair_call_is_shown_the_actual_validation_error(generator_with_responses):
    """A repair prompt without the reason is just a resample. The
    rejection message and the rejected script both have to reach the
    model or the loop has no advantage over retrying.
    """
    bad = _script_payload(ending_text="what this video will cover")
    good = _script_payload(ending_text="what Rome left behind")

    generator, _provider, context, patches = generator_with_responses([bad, good])
    _run(generator, context, patches)

    render_kwargs = [
        call.kwargs
        for call in generator._prompts.get.return_value.render.call_args_list
        if "validation_error" in call.kwargs
    ]
    assert render_kwargs, "the repair prompt was never rendered with a validation_error"
    assert "repeats" in render_kwargs[0]["validation_error"]
    assert "what this video will cover" in render_kwargs[0]["rejected_script"]


def test_repair_attempts_are_bounded_and_the_original_defect_is_reported(
    generator_with_responses,
):
    """A model that keeps returning the same broken script must not loop
    forever, and the error the job fails with must still describe the
    script's real problem.
    """
    bad = _script_payload(ending_text="what this video will cover")

    generator, provider, context, patches = generator_with_responses(
        [copy.deepcopy(bad) for _ in range(4)], repair_attempts=2
    )
    with pytest.raises(ScriptGenerationError, match="repeats"):
        _run(generator, context, patches)

    # 1 generate + exactly 2 repairs, then it gives up.
    assert provider.generate_tool_call.call_count == 3


def test_repair_can_be_disabled_entirely(generator_with_responses):
    """Zero attempts restores the previous behavior exactly — one call,
    fail honestly on rejection — so the loop is opt-out, not mandatory.
    """
    bad = _script_payload(ending_text="what this video will cover")

    generator, provider, context, patches = generator_with_responses(
        [copy.deepcopy(bad) for _ in range(3)], repair_attempts=0
    )
    with pytest.raises(ScriptGenerationError, match="repeats"):
        _run(generator, context, patches)
    assert provider.generate_tool_call.call_count == 1


def test_a_valid_script_never_triggers_a_repair_call(generator_with_responses):
    good = _script_payload(ending_text="what Rome left behind")
    generator, provider, context, patches = generator_with_responses([good])
    script = _run(generator, context, patches)

    assert script.ending.voiceover_text == "what Rome left behind"
    assert provider.generate_tool_call.call_count == 1


def test_repair_cannot_launder_an_invalid_script_through(generator_with_responses):
    """The point that matters most: repair re-runs the same validation.
    A "fixed" script that is still invalid — here the model moves the
    duplication onto a different beat — must still be rejected.
    """
    bad = _script_payload(ending_text="what this video will cover")
    still_bad = _script_payload(ending_text="a striking opening question")  # now dupes the hook

    generator, provider, context, patches = generator_with_responses(
        [bad, still_bad, copy.deepcopy(still_bad)], repair_attempts=2
    )
    with pytest.raises(ScriptGenerationError, match="repeats"):
        _run(generator, context, patches)
    assert provider.generate_tool_call.call_count == 3


def test_a_failing_repair_call_does_not_hide_the_scripts_real_defect(
    generator_with_responses,
):
    """If the provider dies mid-repair, the job must report both: the
    validation failure (the script's actual problem) and the outage, so
    an infrastructure fault is never mistaken for a bad script.
    """
    from libs.providers.llm.base import LLMProviderError

    bad = _script_payload(ending_text="what this video will cover")
    generator, provider, context, patches = generator_with_responses([bad])
    provider.generate_tool_call.side_effect = [
        mock.Mock(tool_input=bad, input_tokens=1, output_tokens=1),
        LLMProviderError("connection refused"),
    ]

    with pytest.raises(ScriptGenerationError) as excinfo:
        _run(generator, context, patches)
    assert "repeats" in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)
