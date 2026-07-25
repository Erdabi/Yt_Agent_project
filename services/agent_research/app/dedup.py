"""Duplicate-content check for newly proposed video ideas.

This is a lexical heuristic — title similarity (`difflib.SequenceMatcher`)
and keyword-set overlap (Jaccard) — not the embedding-based pgvector
similarity search described in the original design doc
(docs/architecture/04-database-design.md §4.3: "checks each candidate for
semantic duplication ... embedding similarity, pgvector"). That approach
needs a real embedding provider, and none is configured yet — no
`embedding` capability exists in libs/providers today, and
`VideoIdea.embedding` stays null until one is wired in.

Being honest about the gap: this catches near-exact repeats and reworded
titles that share most of their keywords, but it will miss a true semantic
duplicate phrased completely differently (e.g. "Best budget laptops 2026"
vs. "Top affordable laptops this year") — a real embedding model would
catch that, this doesn't. Good enough to flag the obvious case now;
swapping in real embedding similarity later is a matter of adding an
`embedding` provider capability and querying `video_ideas.embedding` with
pgvector's cosine-distance operator instead of this module — the call
site (idea_generator.py's caller) wouldn't need to change.
"""

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class DuplicateMatch:
    idea_id: str
    title: str
    similarity: float


class DuplicateChecker:
    def __init__(self, threshold: float = 0.6) -> None:
        self._threshold = threshold

    def find_best_match(
        self,
        topic: str,
        keywords: list[str],
        existing: list[tuple[str, str, list[str]]],
    ) -> DuplicateMatch | None:
        """`existing` is a list of (idea_id, title, keywords) tuples — not
        ORM objects — so this stays usable outside a live DB session and
        easy to unit test with plain data.
        """
        best: DuplicateMatch | None = None
        norm_topic = topic.strip().lower()
        keyword_set = {k.strip().lower() for k in keywords if k.strip()}

        for idea_id, title, existing_keywords in existing:
            title_similarity = difflib.SequenceMatcher(
                None, norm_topic, title.strip().lower()
            ).ratio()

            existing_keyword_set = {k.strip().lower() for k in (existing_keywords or []) if k.strip()}
            union = keyword_set | existing_keyword_set
            keyword_similarity = len(keyword_set & existing_keyword_set) / len(union) if union else 0.0

            similarity = max(title_similarity, keyword_similarity)
            if similarity >= self._threshold and (best is None or similarity > best.similarity):
                best = DuplicateMatch(idea_id=idea_id, title=title, similarity=similarity)

        return best
