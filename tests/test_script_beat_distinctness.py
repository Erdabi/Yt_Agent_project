"""Tests for the Script Agent's beat-distinctness validation.

A degenerating model can satisfy every per-beat rule in the script
schema while emitting the same paragraph several times over — each copy
independently well-formed. Observed for real: a 21-beat script with only
8 distinct narration texts, which downstream collapsed onto 8 narration
clips and 5 images and produced a video that visibly looped. These tests
pin the rule that rejects that shape, and — just as importantly — pin
that it does *not* reject legitimately varied scripts.
"""

import pytest

from services.agent_scriptwriter.app.script_schema import script_from_dict

_PRODUCTION = {
    "camera_framing": "wide shot",
    "asset_requirements": [
        {"asset_type": "ai_image", "description": "a harbour at dawn"}
    ],
    "transition_type": "fade",
    "pacing": "medium",
    "narration_emotion": "curious",
    "emphasis_words": [],
    "estimated_speech_wpm": 150,
}


def _beat(voiceover_text: str) -> dict:
    return {
        "voiceover_text": voiceover_text,
        "scene_description": "something happens on screen",
        "visual_suggestions": "a relevant establishing shot",
        "production_metadata": dict(_PRODUCTION),
    }


def _script(main_section_texts: list[str], **overrides) -> dict:
    raw = {
        "structure_notes": "chronological",
        "retention_notes": "open loop in the hook",
        "hook": _beat("Nobody expected a goat herder to change the world economy."),
        "introduction": _beat(
            "Today we trace how one bitter berry travelled from Ethiopian hills to every kitchen."
        ),
        "main_sections": [
            {**_beat(text), "heading": f"Section {index}"}
            for index, text in enumerate(main_section_texts)
        ],
        "ending": _beat("From a hillside curiosity to a global ritual in six centuries."),
        "call_to_action": _beat("Subscribe if you want the story of tea next week."),
    }
    raw.update(overrides)
    return raw


# --- the rejected shape ------------------------------------------------------


def test_verbatim_repeated_beat_is_rejected():
    duplicated = (
        "Merchants carried the beans north across the desert, and within a decade "
        "the trade had reshaped the entire coastline of the peninsula."
    )
    with pytest.raises(ValueError, match="repeats"):
        script_from_dict(_script([duplicated, "Something entirely different happened next.", duplicated]))


def test_near_verbatim_reworded_beat_is_rejected():
    """The observed failure was rarely an exact copy — it was the same
    sentences lightly reshuffled, which an equality check would miss.
    """
    original = (
        "Merchants carried the roasted beans north across the desert and within a "
        "single decade the growing trade had reshaped the entire coastline forever."
    )
    reworded = (
        "Within a single decade the growing trade had reshaped the entire coastline "
        "forever as merchants carried the roasted beans north across the desert."
    )
    with pytest.raises(ValueError, match="near-identical"):
        script_from_dict(_script([original, reworded]))


def test_error_names_both_offending_beats():
    """A rejection has to be actionable: the Manager's retry and any
    human reading `jobs.error` need to know *which* beats collided.
    """
    duplicated = (
        "The first coffeehouses opened their doors and immediately became the noisiest "
        "rooms in the entire city, packed with argument from dawn until midnight."
    )
    with pytest.raises(ValueError) as exc_info:
        script_from_dict(_script([duplicated, duplicated]))
    message = str(exc_info.value)
    assert "main_sections[0]" in message and "main_sections[1]" in message


# --- the accepted shape ------------------------------------------------------


def test_a_genuinely_varied_script_passes():
    script = script_from_dict(
        _script(
            [
                "Legend says a goat herder noticed his animals dancing after eating red berries.",
                "Sufi monks in Yemen brewed the beans to stay awake through long night prayers.",
                "Ottoman coffeehouses became so politically charged that sultans tried banning them.",
                "Venetian traders carried the first sacks into Europe against fierce suspicion.",
            ]
        )
    )
    assert len(script.main_sections) == 4


def test_short_beats_sharing_common_words_are_not_false_positives():
    """A hook and a call to action are both short and both about the
    same subject — they will share most of their small vocabulary
    without either being a copy. Rejecting those would make the
    validation unusable.
    """
    script = script_from_dict(
        _script(
            ["Coffee spread west along established trade routes."],
            hook=_beat("Coffee changed the world."),
            call_to_action=_beat("Coffee changed history — subscribe."),
        )
    )
    assert script.hook.voiceover_text != script.call_to_action.voiceover_text


def test_beats_on_the_same_topic_with_different_content_pass():
    """Every beat in a focused script shares topic vocabulary — that
    alone must not trip the check, or no on-topic script could pass.
    """
    script = script_from_dict(
        _script(
            [
                "Coffee arrived in Mecca where the first public coffeehouses drew crowds nightly.",
                "Coffee reached Venice by sixteen hundred where physicians debated whether it was poison.",
                "Coffee plantations spread through the Caribbean on the backs of enslaved labourers.",
            ]
        )
    )
    assert len(script.main_sections) == 3


# --- interaction with the existing asset-coverage rule ----------------------


def test_asset_coverage_validation_still_applies():
    """The distinctness rule is additive — it must not have displaced
    the coverage rule that was already there.
    """
    raw = _script(["A perfectly distinct and unique sentence about harbours."])
    raw["main_sections"][0]["production_metadata"]["asset_requirements"] = [
        {"asset_type": "sound_effect", "description": "gulls"}
    ]
    with pytest.raises(ValueError, match="no corresponding visual asset"):
        script_from_dict(raw)
