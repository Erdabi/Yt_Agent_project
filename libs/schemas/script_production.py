"""The shape of one script segment's production metadata — the contract
between the Script Agent (services/agent_scriptwriter), which produces
it, and the Video Agent (services/agent_video), which consumes it to
plan and generate concrete media assets. Lives in `libs/schemas/` rather
than inside either service's own `app/` package because both services
need it: per docs/architecture/02-folder-structure.md, `libs/` is the
only place cross-service code lives — a service never imports another
service's `app/` code directly, only shared contracts declared here,
the same way `libs.schemas.knowledge.KnowledgePackage` is the shared
contract between the Research Agent and the Script Agent.

Stored as `ScriptSegment.production_metadata` (JSONB — see
libs/models/script.py); `services/agent_scriptwriter/app/script_schema.py`
builds Claude's tool-call schema from these types, and
`services/agent_video/app/pipeline_schema.py` parses them back out with
`SegmentProductionMetadata.model_validate(segment.production_metadata)`.
"""

import enum

from pydantic import BaseModel, ConfigDict, Field


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


class AssetType(str, enum.Enum):
    """What kind of production asset a beat needs — the full vocabulary a
    script can draw from, independent of any specific provider. Distinct
    from `libs.models.enums.ShotType` (used by `StoryboardShot.shot_type`
    once the Video Agent's Asset Planning module resolves a *visual*
    requirement into one concrete shot): this is the broader, upstream
    "what's needed" the Script Agent declares, covering audio as well as
    several visual treatments `ShotType` doesn't distinguish. Translating
    a visual `AssetType` down to a `ShotType`, and to a `libs.providers`
    capability, is Asset Planning's job
    (services/agent_video/app/pipeline_schema.py's `ASSET_TYPE_ROUTING`) —
    not this schema's, and not the Script Agent's.
    """

    AI_VIDEO = "ai_video"
    AI_IMAGE = "ai_image"
    STOCK_FOOTAGE = "stock_footage"
    ANIMATION = "animation"
    DIAGRAM = "diagram"
    MAP = "map"
    PORTRAIT = "portrait"
    TEXT_OVERLAY = "text_overlay"
    SUBTITLE_EMPHASIS = "subtitle_emphasis"
    SOUND_EFFECT = "sound_effect"
    BACKGROUND_MUSIC_CUE = "background_music_cue"


#: Every `AssetType` that produces something visible on screen — i.e.
#: every one of them except the two purely audio types. Used both by the
#: Script Agent (`script_schema.py`'s `_validate_asset_coverage`) to check
#: a beat's described visual has something declared to realize it, and by
#: the Video Agent's Asset Planning module to decide which requirements
#: it plans as on-screen shots vs. supplementary audio.
VISUAL_ASSET_TYPES = frozenset(AssetType) - {AssetType.SOUND_EFFECT, AssetType.BACKGROUND_MUSIC_CUE}

#: Which `libs.providers` capability has to be working for each asset
#: type to be producible — `None` for the two that never reach a provider
#: at all (`TEXT_OVERLAY` is composited as on-screen text at render time,
#: `SUBTITLE_EMPHASIS` is read directly by Subtitle Generation), which is
#: why those two are always available whatever the environment looks
#: like.
#:
#: This lives next to `AssetType` itself, rather than in whichever
#: component happens to need it, because it is a property of the
#: vocabulary: naming a new asset type without saying what would produce
#: it leaves every consumer guessing. It is deliberately the *only*
#: place that mapping is written down — the Script Agent validates
#: against it, the CLI derives its production-constraint note from it,
#: and the Video Agent's own routing table is checked against it by
#: test, so the three cannot quietly disagree about what `animation`
#: needs.
ASSET_TYPE_CAPABILITY: dict[AssetType, str | None] = {
    AssetType.AI_VIDEO: "video_gen",
    AssetType.ANIMATION: "video_gen",
    AssetType.AI_IMAGE: "image_gen",
    AssetType.DIAGRAM: "image_gen",
    AssetType.MAP: "image_gen",
    AssetType.PORTRAIT: "image_gen",
    AssetType.STOCK_FOOTAGE: "stock_media",
    AssetType.SOUND_EFFECT: "audio_library",
    AssetType.BACKGROUND_MUSIC_CUE: "audio_library",
    AssetType.TEXT_OVERLAY: None,
    AssetType.SUBTITLE_EMPHASIS: None,
}


class AssetRequirement(BaseModel):
    """One concrete production need for a beat. `description` is content
    — what it should actually show or sound like (e.g. "close-up of a
    chisel bevel at a 25-degree angle", "a soft ambient workshop drone")
    — never a provider, tool, or model name; which one fulfills it is a
    decision for whatever satisfies this requirement later (the Video
    Agent's Asset Planning/Asset Generation modules), not this agent's
    to make.
    """

    model_config = ConfigDict(frozen=True)

    asset_type: AssetType
    description: str


class SegmentProductionMetadata(BaseModel):
    """Everything the Video Agent needs to turn one script beat into
    concrete assets, without additional parsing.
    """

    model_config = ConfigDict(frozen=True)

    camera_framing: str
    #: Every production asset this beat needs — a beat can require more
    #: than one at once (e.g. a stock clip AND a sound effect AND a text
    #: overlay). At least one must be a visual type (`VISUAL_ASSET_TYPES`)
    #: covering what `scene_description`/`visual_suggestions` describes —
    #: enforced by `script_schema.py`'s `_validate_asset_coverage`, not
    #: just prompted for.
    asset_requirements: list[AssetRequirement] = Field(default_factory=list)
    transition_type: TransitionType
    pacing: Pacing
    narration_emotion: str
    emphasis_words: list[str] = Field(default_factory=list)
    estimated_speech_wpm: int
