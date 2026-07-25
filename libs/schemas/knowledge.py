"""The Knowledge Package — the Research Agent's deep-research output for
one video idea, structured so the Script Agent can write from it directly
without repeating the research itself.

Produced by `services/agent_research/app/knowledge_builder.py` and stored
as two files via `libs.storage`, keyed by project id
(`{project_id}/research/knowledge_package.json` /
`.../knowledge_package.md`) — the JSON is the source of truth a consumer
parses, the Markdown is a human-readable rendering generated from it
(`services/agent_research/app/knowledge_package_render.py`), never a
separately hand-maintained copy. `VideoIdea.knowledge_package_json_path`
/ `.knowledge_package_md_path` (libs/models/idea.py) point at them.

A future Script Agent loads it with:

    from libs.storage import get_storage_backend
    from libs.schemas.knowledge import KnowledgePackage

    storage = get_storage_backend()
    package = KnowledgePackage.model_validate_json(
        storage.read_bytes(idea.knowledge_package_json_path)
    )
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bumped whenever a field is added/renamed/removed in a way a consumer
#: needs to branch on. A consumer that only reads fields it knows about
#: doesn't need to check this for purely-additive changes — Pydantic
#: ignores unknown fields on load either way — but should check it before
#: assuming a field it depends on still means the same thing.
CURRENT_SCHEMA_VERSION = "1.0"


class VerifiedFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement: str
    #: URLs the source material actually came from. Empty when nothing
    #: citable was found — see `confidence` in that case, not an implied
    #: guarantee.
    sources: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class TimelineEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Deliberately a free string, not a date/datetime — historical
    #: precision varies ("1998", "Q3 2023", "March 14, 2024", "undated").
    date: str
    event: str
    sources: list[str] = Field(default_factory=list)


class Entity(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    #: Free string ("person", "organization", "product", "place",
    #: "concept", ...) — deliberately not an enum, so a new kind of entity
    #: never needs a schema change.
    type: str
    description: str


class KnowledgePackage(BaseModel):
    """See module docstring. `extensions` is the deliberate escape hatch
    for future niches: a cooking channel might add
    `extensions={"ingredients": [...], "steps": [...]}`, a tech-review
    channel `extensions={"spec_comparison": {...}}`, a sports-recap
    channel `extensions={"box_score": {...}}` — anything a specific
    content vertical needs, as plain JSON, without a new top-level field
    or a migration every time a new niche is supported. Empty (`{}`) is
    the common case; nothing populates it yet (see knowledge_builder.py)
    but the shape is ready for a future niche-specific builder to.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = CURRENT_SCHEMA_VERSION
    topic: str
    niche: str | None = None
    summary: str
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    #: Every unique URL referenced anywhere in verified_facts/timeline,
    #: in first-seen order — derived programmatically (see
    #: knowledge_builder.py), not asked of the LLM directly, so it can
    #: never disagree with the per-fact/per-entry sources it's built from.
    citations: list[str] = Field(default_factory=list)
    #: Terms central to the research itself — distinct from
    #: `VideoIdea.keywords` (SEO/discovery keywords chosen for the idea).
    keywords: list[str] = Field(default_factory=list)
    related_topics: list[str] = Field(default_factory=list)
    #: Specific, usable opening/angle ideas — not generic advice.
    hooks: list[str] = Field(default_factory=list)
    #: Caveats, source disagreements, research gaps — anything a
    #: consumer should weigh before trusting the package at face value.
    supporting_notes: str = ""
    extensions: dict[str, Any] = Field(default_factory=dict)
