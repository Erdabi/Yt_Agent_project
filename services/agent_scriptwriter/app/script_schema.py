"""The Script Agent's structured output contract — shared by both the
initial draft (script_generator.py) and the self-review pass
(script_reviewer.py), so a script has exactly one structural shape, not
two independently-evolving ones the worker would have to reconcile.

Every segment carries, in addition to narration and visuals, structured
production metadata the Video Agent (services/agent_video) can consume
directly — camera framing, transition, pacing, narration emotion,
emphasis words, estimated speech speed, and a structured list of asset
requirements — without parsing free text out of a notes column. That
metadata's shape (`SegmentProductionMetadata` and friends) lives in
`libs/schemas/script_production.py`, not here, since the Video Agent — a
different service — needs it too, and services only share code through
`libs/`, never each other's `app/` packages.

`asset_requirements` (`AssetRequirement`) is deliberately
*provider-independent*: it declares what kind of asset a beat needs (an
AI-generated video, a stock clip, a diagram, a sound effect, ...) and
what it should contain, never which concrete provider or tool should
supply it — the same separation `libs.providers` already enforces for
capability selection (an agent asks for "the active TTS provider", never
a vendor by name). Resolving a requirement into a concrete asset is the
Video Agent's job (its Asset Planning/Asset Generation modules).
"""

from dataclasses import dataclass

from libs.schemas.knowledge import KnowledgePackage
from libs.schemas.script_production import (
    VISUAL_ASSET_TYPES,
    AssetRequirement,
    AssetType,
    Pacing,
    SegmentProductionMetadata,
    TransitionType,
)


#: Stripped from word edges before comparing two beats' vocabularies
#: (`_validate_beat_distinctness`) so "coffee." and "coffee" count as the
#: same word rather than inflating a repeated beat's apparent novelty.
_PUNCTUATION = ".,!?;:\"'()[]—–-"

#: Vocabulary-overlap fraction at or above which two beats are treated as
#: the same beat restated rather than two genuinely different ones. Set
#: from the observed failure mode: a degenerating model's restatements
#: overlapped well above this, while distinct beats of a real script sit
#: far below it (they share only topic words and function words).
_MAX_BEAT_SIMILARITY = 0.8

#: Beats shorter than this are exempt from the *fuzzy* check above
#: (verbatim repeats are still always rejected). Two genuinely different
#: short beats — a one-line hook and a one-line call to action — can
#: legitimately share most of a small vocabulary without either being a
#: copy, so scoring them would produce false rejections.
_MIN_WORDS_FOR_SIMILARITY_CHECK = 12


@dataclass(frozen=True)
class ScriptBeat:
    voiceover_text: str
    scene_description: str
    visual_suggestions: str
    production: SegmentProductionMetadata


@dataclass(frozen=True)
class ScriptSection(ScriptBeat):
    #: A short internal label (e.g. "Why this happens") — never shown to
    #: viewers, just for readability in the DB/audit trail.
    heading: str


@dataclass(frozen=True)
class GeneratedScript:
    structure_notes: str
    retention_notes: str
    hook: ScriptBeat
    introduction: ScriptBeat
    main_sections: list[ScriptSection]
    ending: ScriptBeat
    call_to_action: ScriptBeat
    #: Populated by script_reviewer.py; empty for a freshly generated,
    #: not-yet-reviewed draft.
    review_notes: str = ""


# --- JSON tool-schema fragments, shared by the generate and review calls -

_ASSET_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_type": {
            "type": "string",
            "enum": [t.value for t in AssetType],
            "description": "What kind of production asset this is.",
        },
        "description": {
            "type": "string",
            "description": (
                "What this asset should actually show or sound like, in "
                'content terms (e.g. "close-up of a chisel bevel at a '
                '25-degree angle", "a map highlighting the Pacific '
                'Northwest", "a soft ambient workshop drone") — never '
                "which provider or tool should make it; that is not your "
                "decision."
            ),
        },
    },
    "required": ["asset_type", "description"],
    "additionalProperties": False,
}

_PRODUCTION_METADATA_PROPERTIES = {
    "camera_framing": {
        "type": "string",
        "description": (
            'The camera shot type for this beat (e.g. "wide shot", "medium '
            'shot", "close-up", "extreme close-up", "over-the-shoulder", '
            '"point-of-view", "aerial") — concrete, not "a nice shot."'
        ),
    },
    "asset_requirements": {
        "type": "array",
        "items": _ASSET_REQUIREMENT_SCHEMA,
        "minItems": 1,
        "description": (
            "Every concrete production asset this beat needs, independent "
            "of which provider ultimately supplies it. A beat can need "
            "more than one at once (e.g. a stock video clip AND a sound "
            "effect AND a text overlay). At least one entry must be a "
            "visual asset (any asset_type other than sound_effect or "
            "background_music_cue) that covers what scene_description/"
            "visual_suggestions describes — a beat cannot describe a "
            "visual with nothing declared to realize it."
        ),
    },
    "transition_type": {
        "type": "string",
        "enum": [t.value for t in TransitionType],
        "description": "How this beat transitions in from the previous one.",
    },
    "pacing": {
        "type": "string",
        "enum": [p.value for p in Pacing],
        "description": (
            "The editing rhythm/cut frequency for this beat — how quickly "
            "visuals should change, independent of narration speed."
        ),
    },
    "narration_emotion": {
        "type": "string",
        "description": (
            'The vocal tone/emotion the voice-over should convey (e.g. '
            '"curious", "urgent", "reassuring", "playful", "serious") — '
            'concrete, not "engaging."'
        ),
    },
    "emphasis_words": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Exact words/short phrases from this beat's voiceover_text that "
            "should be vocally emphasized or visually highlighted in "
            "captions. Empty if none stand out."
        ),
    },
    "estimated_speech_wpm": {
        "type": "integer",
        "minimum": 80,
        "maximum": 220,
        "description": (
            "Estimated narration speed in words per minute for this beat "
            "(typically 130-170) — slower for weighty/serious moments, "
            "faster for punchy/urgent ones."
        ),
    },
}
_PRODUCTION_METADATA_REQUIRED = list(_PRODUCTION_METADATA_PROPERTIES.keys())

PRODUCTION_METADATA_SCHEMA = {
    "type": "object",
    "properties": _PRODUCTION_METADATA_PROPERTIES,
    "required": _PRODUCTION_METADATA_REQUIRED,
    "additionalProperties": False,
}

_BEAT_PROPERTIES = {
    "voiceover_text": {
        "type": "string",
        "description": (
            "Exact narration for this beat, written for spoken delivery — "
            "short sentences, natural rhythm, not prose meant to be read."
        ),
    },
    "scene_description": {
        "type": "string",
        "description": "What is happening on screen during this beat, for whoever storyboards it next.",
    },
    "visual_suggestions": {
        "type": "string",
        "description": (
            "Concrete visual ideas for this beat (b-roll, on-screen text, a "
            'specific shot) — never a generic "add engaging visuals."'
        ),
    },
    "production_metadata": PRODUCTION_METADATA_SCHEMA,
}
_BEAT_REQUIRED = list(_BEAT_PROPERTIES.keys())

BEAT_SCHEMA = {
    "type": "object",
    "properties": _BEAT_PROPERTIES,
    "required": _BEAT_REQUIRED,
    "additionalProperties": False,
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {
            "type": "string",
            "description": "A short internal label for this section (not shown to viewers).",
        },
        **_BEAT_PROPERTIES,
    },
    "required": ["heading", *_BEAT_REQUIRED],
    "additionalProperties": False,
}

#: The full script content — shared by `propose_script`
#: (script_generator.py) and `submit_reviewed_script` (script_reviewer.py).
#: The reviewer's tool adds one more property (`review_notes`) on top of
#: this; the generator's does not.
SCRIPT_CONTENT_PROPERTIES = {
    "structure_notes": {
        "type": "string",
        "description": (
            "The narrative structure/story arc chosen for this script "
            '(e.g. "problem -> agitation -> solution", "chronological '
            'case study", "listicle with a throughline") and why it fits '
            "this topic."
        ),
    },
    "retention_notes": {
        "type": "string",
        "description": (
            "The specific retention techniques used and where in the "
            "script (open loops, pattern interrupts, callbacks, curiosity "
            "gaps) — concrete, tied to moments in the script, not generic "
            "advice."
        ),
    },
    "hook": BEAT_SCHEMA,
    "introduction": BEAT_SCHEMA,
    "main_sections": {
        "type": "array",
        "items": SECTION_SCHEMA,
        "minItems": 1,
    },
    "ending": BEAT_SCHEMA,
    "call_to_action": BEAT_SCHEMA,
}
SCRIPT_CONTENT_REQUIRED = list(SCRIPT_CONTENT_PROPERTIES.keys())


def render_knowledge_package_context(package: KnowledgePackage) -> str:
    """A compact plain-text digest of a `KnowledgePackage` for a prompt —
    not the full Markdown rendering
    (services/agent_research/app/knowledge_package_render.py), which is
    Research-Agent-specific output for humans; this only needs the
    content-generation/verification inputs (facts, timeline, entities,
    hooks), not citations or schema metadata.
    """
    lines: list[str] = [f"Summary: {package.summary}"]

    if package.verified_facts:
        lines.append("Verified facts:")
        lines.extend(
            f"- {fact.statement} ({fact.confidence} confidence)" for fact in package.verified_facts
        )
    if package.timeline:
        lines.append("Timeline:")
        lines.extend(f"- {entry.date}: {entry.event}" for entry in package.timeline)
    if package.entities:
        lines.append("Key entities:")
        lines.extend(f"- {e.name} ({e.type}): {e.description}" for e in package.entities)
    if package.hooks:
        lines.append("Suggested hooks from research:")
        lines.extend(f"- {hook}" for hook in package.hooks)
    if package.related_topics:
        lines.append(f"Related topics: {', '.join(package.related_topics)}")
    if package.supporting_notes:
        lines.append(f"Caveats/notes: {package.supporting_notes}")

    return "\n".join(lines)


# --- Conversion: raw tool-call dict <-> GeneratedScript -------------------


def _beat_from_dict(data: dict) -> ScriptBeat:
    return ScriptBeat(
        voiceover_text=data["voiceover_text"],
        scene_description=data["scene_description"],
        visual_suggestions=data["visual_suggestions"],
        production=SegmentProductionMetadata.model_validate(data["production_metadata"]),
    )


def _section_from_dict(data: dict) -> ScriptSection:
    beat = _beat_from_dict(data)
    return ScriptSection(
        voiceover_text=beat.voiceover_text,
        scene_description=beat.scene_description,
        visual_suggestions=beat.visual_suggestions,
        production=beat.production,
        heading=data["heading"],
    )


def _beat_covers_visual(beat: ScriptBeat) -> bool:
    return any(req.asset_type in VISUAL_ASSET_TYPES for req in beat.production.asset_requirements)


def _labelled_beats(script: GeneratedScript) -> list[tuple[str, ScriptBeat]]:
    """Every beat in narration order, each with the human-readable label
    used in validation errors. Shared by the validators below so they
    can't drift apart on what counts as "a beat".
    """
    return [
        ("hook", script.hook),
        ("introduction", script.introduction),
        *(
            (f'main_sections[{i}] ("{section.heading}")', section)
            for i, section in enumerate(script.main_sections)
        ),
        ("ending", script.ending),
        ("call_to_action", script.call_to_action),
    ]


def _normalized_words(text: str) -> list[str]:
    return [word.strip(_PUNCTUATION).lower() for word in text.split() if word.strip(_PUNCTUATION)]


def _similarity(left: set[str], right: set[str]) -> float:
    """Jaccard overlap of two beats' vocabularies. Deliberately a *set*
    comparison rather than a sequence diff: a model that degenerates into
    repetition reuses the same words in near-identical proportions, and
    word order is exactly the thing that varies slightly between one
    restatement and the next — so ordering-insensitive overlap is what
    actually catches it.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _validate_beat_distinctness(script: GeneratedScript) -> None:
    """Every beat must say something new.

    A weak or degenerating model can satisfy every other constraint in
    this schema while emitting the *same paragraph* several times over —
    each copy independently well-formed, so no per-beat rule catches it.
    That failure is invisible here but catastrophic downstream: identical
    narration text hits the Asset Cache's content-addressed key
    (services/agent_video/app/asset_cache.py), so duplicated beats
    collapse onto literally the same narration audio *and* the same
    generated image, and the finished video visibly loops. This is the
    only place in the pipeline that can see the whole script at once, so
    it's the only place that can reject that shape.

    Raises `ValueError`, handled exactly like `_validate_asset_coverage`'s:
    `generate()` turns it into a hard `ScriptGenerationError`; `review()`
    falls back to the prior, already-valid draft.
    """
    seen: list[tuple[str, list[str], set[str]]] = []
    for label, beat in _labelled_beats(script):
        words = _normalized_words(beat.voiceover_text)
        vocabulary = set(words)
        for prior_label, prior_words, prior_vocabulary in seen:
            if words and words == prior_words:
                raise ValueError(
                    f"{label} repeats {prior_label} verbatim — every beat must advance the "
                    "script with new narration, not restate an earlier one"
                )
            # Only fuzzy-match beats with enough substance for the
            # overlap score to mean anything: two short beats (a hook, a
            # call to action) can legitimately share most of their small
            # vocabulary without either being a copy of the other.
            if (
                len(words) >= _MIN_WORDS_FOR_SIMILARITY_CHECK
                and len(prior_words) >= _MIN_WORDS_FOR_SIMILARITY_CHECK
                and _similarity(vocabulary, prior_vocabulary) >= _MAX_BEAT_SIMILARITY
            ):
                raise ValueError(
                    f"{label} is near-identical to {prior_label} "
                    f"({_similarity(vocabulary, prior_vocabulary):.0%} vocabulary overlap) — "
                    "every beat must advance the script with new narration"
                )
        seen.append((label, words, vocabulary))


def _validate_asset_coverage(script: GeneratedScript) -> None:
    """Every beat's `scene_description`/`visual_suggestions` describes a
    visual — both are required, non-empty fields for every beat (see
    `_BEAT_PROPERTIES`) — so every beat must declare at least one
    visual-category asset requirement to actually realize it. The JSON
    schema's `minItems: 1` on `asset_requirements` only guards against an
    empty list; it can't express "and at least one of them must be a
    visual type," so that's enforced here instead, the same way
    `ScriptGenerator`/`ScriptReviewer` already can't rely on JSON schema
    alone for "main_sections must be non-empty" and check it in Python.

    Raises `ValueError` — a caller decides what that means: `generate()`
    turns it into a hard `ScriptGenerationError` (no fallback exists for
    an initial draft); `review()`'s existing exception handling already
    catches it and falls back to the prior, already-valid draft.
    """
    for label, beat in _labelled_beats(script):
        if not _beat_covers_visual(beat):
            raise ValueError(
                f"{label} describes a visual (scene_description/visual_suggestions) "
                "but its asset_requirements has no corresponding visual asset "
                "(only sound_effect/background_music_cue, or none at all)"
            )


def script_from_dict(raw: dict, *, review_notes: str = "") -> GeneratedScript:
    """Parse one `propose_script`/`submit_reviewed_script` tool-call
    payload into a `GeneratedScript`, validating asset coverage and beat
    distinctness before returning it. `review_notes` is supplied by the
    caller since only the review tool's schema includes that field.
    """
    script = GeneratedScript(
        structure_notes=raw["structure_notes"],
        retention_notes=raw["retention_notes"],
        hook=_beat_from_dict(raw["hook"]),
        introduction=_beat_from_dict(raw["introduction"]),
        main_sections=[_section_from_dict(section) for section in raw["main_sections"]],
        ending=_beat_from_dict(raw["ending"]),
        call_to_action=_beat_from_dict(raw["call_to_action"]),
        review_notes=review_notes,
    )
    _validate_asset_coverage(script)
    _validate_beat_distinctness(script)
    return script


def _beat_to_dict(beat: ScriptBeat) -> dict:
    return {
        "voiceover_text": beat.voiceover_text,
        "scene_description": beat.scene_description,
        "visual_suggestions": beat.visual_suggestions,
        "production_metadata": beat.production.model_dump(mode="json"),
    }


def _section_to_dict(section: ScriptSection) -> dict:
    return {"heading": section.heading, **_beat_to_dict(section)}


def script_to_dict(script: GeneratedScript) -> dict:
    """The inverse of `script_from_dict`, minus `review_notes` — used to
    serialize a draft for the reviewer to read, in the exact same shape
    it will return its own revision in.
    """
    return {
        "structure_notes": script.structure_notes,
        "retention_notes": script.retention_notes,
        "hook": _beat_to_dict(script.hook),
        "introduction": _beat_to_dict(script.introduction),
        "main_sections": [_section_to_dict(section) for section in script.main_sections],
        "ending": _beat_to_dict(script.ending),
        "call_to_action": _beat_to_dict(script.call_to_action),
    }
