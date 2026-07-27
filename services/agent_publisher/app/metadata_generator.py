"""Claude-backed publish metadata refinement for the Publisher Agent.

Turns a project's working title/description draft, research context, and
finished script into a refined title, description, and tags, plus a
playlist *theme* suggestion — never a real playlist ID, since only the
calling agent's own channel configuration knows which playlists actually
exist on the channel (see libs/providers/youtube/base.py's
`VideoMetadata.playlist_id`).

Routed through the Provider Registry's `llm` capability
(`libs.providers.get_provider("llm")`), the same reuse
services/agent_qa/app/llm_review.py established for the Quality Control
Agent's reviewers — not a hardcoded `anthropic` import.

A provider failure never falls back to a fabricated title/description —
same no-fabricated-fallback rule every other LLM-backed generator in this
codebase follows (services/agent_research/app/idea_generator.py,
services/agent_video/app/thumbnail_concept_generator.py,
services/agent_qa/app/llm_review.py). Publishing with an invented title
would defeat the entire purpose of this step.
"""

from dataclasses import dataclass

from libs.core.config import get_settings
from libs.llm_usage import track_llm_call
from libs.prompts import PromptNotFoundError, PromptRenderError, get_prompt_loader
from libs.providers.llm.base import LLMProvider, LLMProviderError, LLMToolCall
from libs.providers.registry import get_provider

#: This module only ever calls the Anthropic-backed `llm` provider by
#: default, so it always asks for the "claude" prompt variant if one
#: exists — same rationale as every other forced-tool-use call site in
#: this codebase (e.g. services/agent_qa/app/reviewers/script_reviewer.py).
_PROMPT_PROVIDER = "claude"


class MetadataGenerationError(RuntimeError):
    """Metadata refinement could not produce a result. Never caught
    silently inside this module — always propagates so the Publisher
    Agent's job fails honestly instead of publishing a video with a
    fabricated/placeholder title.
    """


@dataclass(frozen=True)
class RefinedMetadata:
    title: str
    description: str
    tags: list[str]
    #: A short theme/series label (e.g. "beginner python tutorials"),
    #: never a real playlist ID — `None` when the model found no natural
    #: theme. Resolving this to an actual `playlist_id` is the Publisher
    #: Agent's job, using the channel's own configuration.
    suggested_playlist_theme: str | None


_REFINE_METADATA_TOOL = LLMToolCall(
    name="refine_publish_metadata",
    description=(
        "Report the refined title, description, tags, and playlist theme "
        "for a video about to be published to YouTube."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "The refined video title, at most 100 characters.",
            },
            "description": {
                "type": "string",
                "description": "The refined video description.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Relevant search tags, no leading '#'.",
            },
            "suggested_playlist_theme": {
                "type": "string",
                "description": (
                    "A short theme/series label this video belongs to — never a "
                    "real playlist name or ID. Empty string if no natural theme applies."
                ),
            },
        },
        "required": ["title", "description", "tags", "suggested_playlist_theme"],
        "additionalProperties": False,
    },
)


class MetadataGenerator:
    def __init__(self) -> None:
        self._prompt_version = get_settings().publish_prompt_version
        self._prompts = get_prompt_loader()

    def generate(
        self,
        *,
        project_id: str,
        video_title: str,
        channel_niche: str,
        channel_persona: str,
        target_audience: str | None,
        suggested_angle: str | None,
        research_keywords: list[str] | None,
        draft_description: str | None,
        script_content: str,
    ) -> RefinedMetadata:
        provider: LLMProvider = get_provider("llm")

        try:
            system_template = self._prompts.get(
                "publish", "refine_metadata_system",
                version=self._prompt_version, provider=_PROMPT_PROVIDER,
            )
            user_prompt = self._prompts.get(
                "publish", "refine_metadata_user", version=self._prompt_version,
            ).render(
                video_title=video_title,
                channel_niche=channel_niche,
                channel_persona=channel_persona,
                target_audience=target_audience,
                suggested_angle=suggested_angle,
                research_keywords=research_keywords,
                draft_description=draft_description,
                script_content=script_content,
            )
        except (PromptNotFoundError, PromptRenderError) as exc:
            raise MetadataGenerationError(f"prompt template error: {exc}") from exc

        try:
            with track_llm_call(
                project_id=project_id,
                agent_name="publish",
                call_site="metadata_generator.generate",
                model=provider.model,
                prompt_name="refine_metadata",
                prompt_version=system_template.version,
            ) as usage:
                result = provider.generate_tool_call(
                    system_prompt=system_template.render(),
                    user_prompt=user_prompt,
                    tool=_REFINE_METADATA_TOOL,
                )
                usage["input_tokens"] = result.input_tokens
                usage["output_tokens"] = result.output_tokens
        except LLMProviderError as exc:
            raise MetadataGenerationError(f"LLM metadata refinement failed: {exc}") from exc

        return RefinedMetadata(
            title=result.tool_input["title"],
            description=result.tool_input["description"],
            tags=list(result.tool_input.get("tags") or []),
            suggested_playlist_theme=result.tool_input.get("suggested_playlist_theme") or None,
        )
