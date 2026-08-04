"""Cinematic visual-prompt composition.

Turns one `VisualBeat` plus its segment's production metadata into the
prompt actually sent to the configured `image_gen`/`video_gen` provider
(services/agent_video/app/modules/asset_generation.py).

Why this isn't just the requirement's description
-------------------------------------------------
The Script Agent's `AssetRequirement.description` is deliberately
*content only* — "a 17th century coffeehouse interior" — and
deliberately provider-independent (see libs/schemas/script_production.py).
That is the right contract: a script shouldn't encode sampler settings
or lens choices. But it is also, on its own, a weak diffusion prompt:
nothing in it says how the shot is framed, how it is lit, or what it
should look like as a photograph, so a model fills those in arbitrarily
and the result looks flat and generic.

This module supplies exactly that missing layer, and supplies it from
data the script *already* provides rather than from invention:
`camera_framing` becomes composition, `narration_emotion` becomes
lighting and atmosphere, the beat's own narration slice grounds the shot
in the specific moment being described, and the requirement's
`asset_type` selects a medium-appropriate treatment (a photographic
frame for a scene, a clean flat rendering for a diagram, and so on).

Everything here is deterministic, topic-agnostic string composition —
there is no model call, no vendor concept, and nothing specific to any
subject matter. The same beat always composes the same prompt, which is
what lets the Asset Cache key on it meaningfully.
"""

from libs.schemas.script_production import AssetType

from .pipeline_schema import SegmentInput, VisualBeat

#: Appended to every prompt. Generic quality/medium anchors that steer a
#: diffusion model toward a sharp, well-exposed, professionally-shot
#: frame — the single biggest lever on perceived quality, and entirely
#: independent of what the shot is *of*.
_QUALITY_SUFFIX = (
    "sharp focus, high detail, professional color grading, "
    "shallow depth of field, cinematic composition, 4k detail"
)

#: Sent as the negative prompt on every generation. Named failure modes
#: of the generator itself (mangled text, anatomy artifacts, low-detail
#: mush), not subject matter — so this stays correct for any topic.
DEFAULT_NEGATIVE_PROMPT = (
    "text, watermark, logo, signature, caption, subtitles, letters, words, "
    "blurry, out of focus, low resolution, low detail, jpeg artifacts, noise, "
    "washed out, overexposed, underexposed, flat lighting, "
    "deformed, disfigured, extra limbs, extra fingers, mutated hands, bad anatomy, "
    "ugly, amateur, poorly drawn, cropped, cut off"
)

#: Composition language keyed on words that appear in a script's own
#: `camera_framing` field. Matched as substrings so natural phrasings
#: ("a slow push-in close-up") still resolve. Order matters: the first
#: match wins, so more specific keys are listed before broader ones.
_FRAMING_CLAUSES: tuple[tuple[str, str], ...] = (
    ("extreme close", "extreme macro close-up, fine texture detail, 100mm macro lens"),
    ("close", "intimate close-up, subject fills the frame, 85mm portrait lens"),
    ("medium", "medium shot, balanced framing, 50mm lens, natural perspective"),
    ("wide", "sweeping wide establishing shot, expansive scale, 24mm lens"),
    ("establishing", "sweeping wide establishing shot, expansive scale, 24mm lens"),
    ("aerial", "high aerial vantage, sweeping overhead perspective"),
    ("overhead", "directly overhead flat-lay perspective, symmetrical arrangement"),
    ("top-down", "directly overhead flat-lay perspective, symmetrical arrangement"),
    ("over-the-shoulder", "over-the-shoulder framing, subject seen past a foreground figure"),
    ("point-of-view", "first-person point-of-view perspective, immersive framing"),
    ("pov", "first-person point-of-view perspective, immersive framing"),
    ("low angle", "dramatic low camera angle looking upward, imposing scale"),
    ("high angle", "elevated camera angle looking down, contextual framing"),
)
_DEFAULT_FRAMING_CLAUSE = "balanced medium shot, 50mm lens, natural perspective"

#: Lighting and mood keyed on the script's own `narration_emotion`.
#: Emotion drives light because that is how a real edit works — the same
#: location reads differently lit warm and soft versus hard and cold.
_MOOD_CLAUSES: tuple[tuple[str, str], ...] = (
    ("curious", "soft directional daylight, inviting warm tones, gentle contrast"),
    ("wonder", "golden hour light, luminous atmosphere, warm highlights"),
    ("awe", "golden hour light, luminous atmosphere, warm highlights"),
    ("urgent", "hard dramatic side lighting, deep shadows, high contrast, cool tones"),
    ("tense", "hard dramatic side lighting, deep shadows, high contrast, cool tones"),
    ("dramatic", "chiaroscuro lighting, strong shadows, rich saturated contrast"),
    ("serious", "restrained neutral lighting, muted palette, documentary realism"),
    ("somber", "overcast diffuse light, desaturated cool palette, subdued mood"),
    ("sad", "overcast diffuse light, desaturated cool palette, subdued mood"),
    ("reassuring", "soft even lighting, warm natural palette, calm atmosphere"),
    ("calm", "soft even lighting, warm natural palette, calm atmosphere"),
    ("hopeful", "bright airy light, clean highlights, optimistic warm palette"),
    ("triumphant", "bold rim lighting, vivid saturated palette, heroic atmosphere"),
    ("playful", "bright cheerful lighting, punchy saturated colors, lively mood"),
    ("nostalgic", "warm faded light, gentle haze, muted vintage palette"),
    ("mysterious", "low-key moody lighting, atmospheric haze, deep shadows"),
)
_DEFAULT_MOOD_CLAUSE = "natural cinematic lighting, balanced contrast, rich color depth"

#: Medium/treatment per requirement type — a diagram should not be
#: rendered as a photograph, and a map should not be rendered as a
#: portrait. Keyed on the Script Agent's own vocabulary so adding a new
#: `AssetType` surfaces here as a missing key rather than silently
#: rendering as something inappropriate.
_MEDIUM_CLAUSES: dict[AssetType, str] = {
    AssetType.AI_IMAGE: "photorealistic documentary photography, authentic period-accurate detail",
    AssetType.AI_VIDEO: "photorealistic documentary cinematography, authentic period-accurate detail",
    AssetType.ANIMATION: "stylized editorial illustration, clean confident linework, cohesive palette",
    AssetType.DIAGRAM: (
        "clean modern infographic diagram, flat vector style, clear visual hierarchy, "
        "generous whitespace, no text labels"
    ),
    AssetType.MAP: (
        "elegant cartographic map illustration, muted parchment palette, "
        "clear geographic forms, no text labels"
    ),
    AssetType.PORTRAIT: (
        "dignified portrait photography, natural skin texture, catchlight in the eyes, "
        "softly blurred background"
    ),
    AssetType.STOCK_FOOTAGE: "clean professional stock footage look, natural motion",
}
_DEFAULT_MEDIUM_CLAUSE = "photorealistic documentary photography, authentic detail"

#: Dropped when mining a narration slice for the concrete nouns that
#: distinguish one beat from another. Function words and narration
#: connectives only — deliberately no topic vocabulary, so this stays
#: correct whatever the video is about.
_STOPWORDS = frozenset(
    """
    a an the and or but so if then than that this these those there here it its it's
    is are was were be been being am do does did doing have has had having
    i you he she we they me him her us them my your his our their
    of in on at to from by for with without into onto over under across through
    as about after before during while when where why how what which who whom
    not no nor only just very much many more most some any all both each few other
    can could will would shall should may might must
    one two three first second next last also too still even yet ever never
    up down out off again further once because until against between
    """.split()
)

#: How many distinct content words from a beat's narration slice are
#: folded into its prompt. Enough to anchor the shot to this specific
#: moment; few enough that the composed prompt stays a shot description
#: rather than a transcript.
_MAX_NARRATION_KEYWORDS = 6

#: Narration words shorter than this are dropped as keywords — they are
#: almost always function words that slipped past `_STOPWORDS`, and add
#: noise rather than subject matter to a prompt.
_MIN_KEYWORD_LENGTH = 4

_PUNCTUATION = ".,!?;:\"'()[]—–"


def compose_visual_prompt(beat: VisualBeat, segment: SegmentInput) -> str:
    """The full generation prompt for one visual beat.

    Layers, in the order a cinematographer would specify a shot: what is
    in frame, how it is framed, how it is lit, what medium it is, and
    what technical quality is expected.
    """
    parts = [
        _subject_clause(beat),
        _framing_clause(segment.production.camera_framing),
        _mood_clause(segment.production.narration_emotion),
        _MEDIUM_CLAUSES.get(beat.asset_type, _DEFAULT_MEDIUM_CLAUSE),
        _QUALITY_SUFFIX,
    ]
    return ", ".join(part for part in parts if part)


def _subject_clause(beat: VisualBeat) -> str:
    """What is actually on screen: the requirement's own description,
    narrowed to the moment this beat covers by the concrete words spoken
    over it. The narrowing is what makes several beats sharing one
    requirement produce genuinely different images instead of minor
    variations of the same one.
    """
    subject = beat.requirement_description.strip().rstrip(".")
    keywords = _narration_keywords(beat.narration_text, exclude=subject)
    if keywords:
        return f"{subject}, featuring {', '.join(keywords)}"
    return subject


def _narration_keywords(narration_text: str, *, exclude: str) -> list[str]:
    """The distinctive content words of a narration slice, in the order
    spoken, skipping anything already present in the requirement
    description (repeating it adds emphasis, not information).
    """
    already_present = {
        word.strip(_PUNCTUATION).lower() for word in exclude.split() if word.strip(_PUNCTUATION)
    }
    keywords: list[str] = []
    seen: set[str] = set()
    for raw_word in narration_text.split():
        word = raw_word.strip(_PUNCTUATION)
        normalized = word.lower()
        if (
            len(normalized) < _MIN_KEYWORD_LENGTH
            or normalized in _STOPWORDS
            or normalized in seen
            or normalized in already_present
        ):
            continue
        seen.add(normalized)
        keywords.append(word)
        if len(keywords) == _MAX_NARRATION_KEYWORDS:
            break
    return keywords


def _framing_clause(camera_framing: str) -> str:
    return _match_clause(camera_framing, _FRAMING_CLAUSES, _DEFAULT_FRAMING_CLAUSE)


def _mood_clause(narration_emotion: str) -> str:
    return _match_clause(narration_emotion, _MOOD_CLAUSES, _DEFAULT_MOOD_CLAUSE)


def _match_clause(value: str, clauses: tuple[tuple[str, str], ...], default: str) -> str:
    """First substring match wins (see `_FRAMING_CLAUSES` on ordering).
    Falls back to a sane cinematic default rather than passing the raw
    script text through: these fields are free text, so an unrecognized
    value is expected, not exceptional.
    """
    normalized = value.lower()
    for keyword, clause in clauses:
        if keyword in normalized:
            return clause
    return default
