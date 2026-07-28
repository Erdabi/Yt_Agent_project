"""Shared LLM-based review helper for the Quality Control Agent's
Script/Video/Thumbnail reviewers (reviewers/) — loading the configured
`llm` provider, forcing a "report issues" tool call, and tracking usage
all happen here once instead of three times.

A provider failure (`libs.providers.llm.base.LLMProviderError` and its
subclasses) is never caught here — it propagates up through the calling
reviewer and `QualityControlAgent.run()` as a genuine job failure, the
same no-fabricated-fallback rule every other LLM-backed generator in this
codebase follows (services/agent_research/app/idea_generator.py,
services/agent_scriptwriter/app/script_generator.py,
services/agent_video/app/thumbnail_concept_generator.py). Inventing a
fake "no issues found" verdict when the reviewer never actually ran would
defeat the entire purpose of quality control.
"""

from typing import Any

from libs.llm_usage import track_llm_call
from libs.providers.llm.base import LLMProvider, LLMToolCall
from libs.providers.registry import get_provider

from .qa_schema import Issue, ReviewResult, build_review_result


def build_issue_reporting_tool(tool_name: str, category_description: str) -> LLMToolCall:
    return LLMToolCall(
        name=tool_name,
        description=f"Report {category_description} findings as a structured summary and itemized issues.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One or two sentence overall assessment of this category.",
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                            "detail": {
                                "type": "string",
                                "description": "What's wrong, specifically.",
                            },
                            "suggested_fix": {
                                "type": "string",
                                "description": "A concrete, actionable instruction to fix it.",
                            },
                        },
                        "required": ["severity", "detail", "suggested_fix"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "issues"],
            "additionalProperties": False,
        },
    )


def run_llm_review(
    *,
    project_id: str,
    category: str,
    tool_name: str,
    category_description: str,
    system_prompt: str,
    user_prompt: str,
    prompt_name: str,
    prompt_version: str,
    images: list[bytes] | None = None,
) -> ReviewResult:
    provider: LLMProvider = get_provider("llm")
    tool = build_issue_reporting_tool(tool_name, category_description)

    with track_llm_call(
        project_id=project_id,
        agent_name="qa",
        call_site=f"{category}_reviewer.review",
        provider=type(provider).__name__,
        model=provider.model,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
    ) as usage:
        result = provider.generate_tool_call(
            system_prompt=system_prompt, user_prompt=user_prompt, tool=tool, images=images,
        )
        usage["input_tokens"] = result.input_tokens
        usage["output_tokens"] = result.output_tokens

    issues_raw: list[dict[str, Any]] = result.tool_input.get("issues", [])
    issues = [
        Issue(
            category=category,
            severity=item["severity"],
            detail=item["detail"],
            suggested_fix=item["suggested_fix"],
        )
        for item in issues_raw
    ]
    summary = result.tool_input.get("summary", "")
    return build_review_result(category, summary, issues)
