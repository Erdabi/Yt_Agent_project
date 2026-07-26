"""The Script Agent's structured output contract — shared by both the
initial draft (script_generator.py) and the self-review pass
(script_reviewer.py), so a script has exactly one structural shape, not
two independently-evolving ones the worker would have to reconcile.

Every segment carries, in addition to narration and visuals, structured
production metadata the Video Agent (services/agent_video) can consume
directly — camera framing, visual asset type, transition, pacing,
narration emotion, emphasis words, estimated speech speed, and optional
on-screen text — without parsing free text out of a notes column.
`visual_asset_type` intentionally reuses `libs.models.enums.ShotType`,
the same vocabulary `StoryboardShot.shot_type` (libs/models/storyboard.py)
already uses, so a future Storyboard module never needs to translate
between two different vocabularies for the same concept.
"""

import enum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from libs.models.enums import ShotType
from libs.schemas.knowledge import KnowledgePackage


class TransitionType(str, enum.Enum):
    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPE = "wipe"
    ZOOM = "zoom"
    SLIDE = "slide"
    MATCH_CUT = "match_cut"


class Pacing(str, enum.Enum):
    """Editing rhythm / cut frequency for a beat — independent of
    narration delivery speed (`SegmentProductionMetadata.estimated_speech_wpm`
    below): a beat can be spoken slowly over fast-cutting visuals, or
    vice versa.
    """

    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


class SegmentProductionMetadata(BaseModel):
    """Everything the Video Agent needs to turn one script beat into a
    concrete shot, without additional parsing.
    """

    model_config = ConfigDict(frozen=True)

    camera_framing: str
    visual_asset_type: ShotType
    transition_type: TransitionType
    pacing: Pacing
    narration_emotion: str
    emphasis_words: list[str] = Field(default_factory=list)
    estimated_speech_wpm: int
    on_screen_text: list[str] = Field(default_factory=list)


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

_PRODUCTION_METADATA_PROPERTIES = {
    "camera_framing": {
        "type": "string",
        "description": (
            'The camera shot type for this beat (e.g. "wide shot", "medium '
            'shot", "close-up", "extreme close-up", "over-the-shoulder", '
            '"point-of-view", "aerial") — concrete, not "a nice shot."'
        ),
    },
    "visual_asset_type": {
        "type": "string",
        "enum": [t.value for t in ShotType],
        "description": "What kind of visual asset should fill this beat.",
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
    "on_screen_text": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Any text overlays for this beat (a title card, a key stat, a "
            "callout) — empty if none."
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


def script_from_dict(raw: dict, *, review_notes: str = "") -> GeneratedScript:
    """Parse one `propose_script`/`submit_reviewed_script` tool-call
    payload into a `GeneratedScript`. `review_notes` is supplied by the
    caller since only the review tool's schema includes that field.
    """
    return GeneratedScript(
        structure_notes=raw["structure_notes"],
        retention_notes=raw["retention_notes"],
        hook=_beat_from_dict(raw["hook"]),
        introduction=_beat_from_dict(raw["introduction"]),
        main_sections=[_section_from_dict(section) for section in raw["main_sections"]],
        ending=_beat_from_dict(raw["ending"]),
        call_to_action=_beat_from_dict(raw["call_to_action"]),
        review_notes=review_notes,
    )


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
