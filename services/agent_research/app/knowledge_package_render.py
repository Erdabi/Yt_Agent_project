"""Renders a `KnowledgePackage` (libs/schemas/knowledge.py) as Markdown —
a human-readable view generated from the JSON model, not a separately
authored copy, so the two can never drift out of sync. The JSON file
remains the source of truth a program parses
(`KnowledgePackage.model_validate_json`); this is for a person (or a
future dashboard) to read.
"""

import json

from libs.schemas.knowledge import KnowledgePackage


def render_markdown(package: KnowledgePackage) -> str:
    lines: list[str] = [f"# {package.topic}", ""]
    if package.niche:
        lines.append(f"*Niche: {package.niche}*")
        lines.append("")

    lines.append("## Summary")
    lines.append(package.summary)
    lines.append("")

    lines.append("## Verified Facts")
    if package.verified_facts:
        for fact in package.verified_facts:
            sources = ", ".join(f"[source]({url})" for url in fact.sources) if fact.sources else "no source found"
            lines.append(f"- ({fact.confidence} confidence) {fact.statement} — {sources}")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## Timeline")
    if package.timeline:
        for entry in package.timeline:
            sources = ", ".join(f"[source]({url})" for url in entry.sources) if entry.sources else "no source found"
            lines.append(f"- **{entry.date}** — {entry.event} ({sources})")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## Entities")
    if package.entities:
        for entity in package.entities:
            lines.append(f"- **{entity.name}** ({entity.type}): {entity.description}")
    else:
        lines.append("_None found._")
    lines.append("")

    lines.append("## Keywords")
    lines.append(", ".join(package.keywords) if package.keywords else "_None._")
    lines.append("")

    lines.append("## Related Topics")
    if package.related_topics:
        for topic in package.related_topics:
            lines.append(f"- {topic}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Hooks")
    if package.hooks:
        for hook in package.hooks:
            lines.append(f"- {hook}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Supporting Notes")
    lines.append(package.supporting_notes or "_None._")
    lines.append("")

    lines.append("## Citations")
    if package.citations:
        for url in package.citations:
            lines.append(f"- {url}")
    else:
        lines.append("_None._")
    lines.append("")

    if package.extensions:
        lines.append("## Extensions")
        lines.append("Niche-specific structured data — see the JSON file for a typed view:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(package.extensions, indent=2))
        lines.append("```")
        lines.append("")

    lines.append(f"---\n_Schema version {package.schema_version}._")
    return "\n".join(lines)
