"""Process-wide cached `PromptLoader`, the same pattern as
`libs.storage.registry.get_storage_backend` and
`libs.providers.registry.get_provider`: one settings-driven root path,
resolved once per process. A restart is required to pick up an edited
prompt file — consistent with how `config/providers.yaml` is already
cached, not a new trade-off this module introduces.
"""

from functools import lru_cache
from pathlib import Path

from libs.core.config import get_settings

from .loader import PromptLoader

# libs/prompts/registry.py -> libs/prompts -> libs -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def get_prompt_loader() -> PromptLoader:
    settings = get_settings()
    root = Path(settings.prompts_root)
    if not root.is_absolute():
        root = _REPO_ROOT / root
    return PromptLoader(root)
