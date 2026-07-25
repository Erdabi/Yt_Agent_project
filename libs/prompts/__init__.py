"""Prompt Management System.

Every prompt sent to an LLM — by the Manager Agent's reasoning engine
today, and by the Research/Script/Video/QA agents once their real LLM
calls land (docs/architecture/06-roadmap.md) — is stored as a versioned
template file under `prompts/` at the repo root, not a Python string
constant. `prompts/README.md` documents the file-naming convention;
loader.py documents the resolution rules in detail.

Usage:

    from libs.prompts import get_prompt_loader

    loader = get_prompt_loader()
    template = loader.get("manager", "workflow_decision_system", provider="claude")
    system_prompt = template.render()

    template = loader.get("manager", "workflow_decision_user")
    user_prompt = template.render(phase="script", stage="scripting", ...)
"""

from .loader import PromptLoader, PromptNotFoundError, PromptRenderError, PromptTemplate
from .registry import get_prompt_loader

__all__ = [
    "PromptLoader",
    "PromptTemplate",
    "PromptNotFoundError",
    "PromptRenderError",
    "get_prompt_loader",
]
