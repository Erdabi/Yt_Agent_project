"""The `Reviewer` interface every specialized reviewer implements.

`QualityControlAgent` (../quality_control_agent.py) holds a plain list of
these — adding a sixth reviewer later (e.g. a content-policy reviewer,
building on the existing prompts/qa/policy_review/ draft — see
docs/architecture/03-agent-responsibilities.md §3.8) means writing one
new class and appending it to that list; nothing in the coordinator or
any existing reviewer changes.
"""

from abc import ABC, abstractmethod

from ..qa_schema import ReviewInput, ReviewResult


class Reviewer(ABC):
    #: Matches the `category` every `Issue` this reviewer produces
    #: carries, and the key `QualityControlAgent` groups issues/review
    #: metadata by.
    category: str

    @abstractmethod
    def review(self, review_input: ReviewInput) -> ReviewResult:
        """Inspect `review_input` and return this category's verdict.
        Must raise (never fabricate a result) if a required LLM call
        fails — see ../llm_review.py's own docstring for the rationale.
        """
