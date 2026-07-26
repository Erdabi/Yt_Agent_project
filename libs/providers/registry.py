"""Config-file-driven provider registry.

Which concrete class backs each swappable capability — video generation,
text-to-speech, image generation, YouTube publishing — is declared once
in `config/providers.yaml` (path configurable via `PROVIDERS_CONFIG_PATH`),
not scattered across agent code. An agent module asks this registry for
"the active provider for `tts`"; it never imports a vendor SDK or a
concrete provider class directly. Swapping ElevenLabs for Azure Speech,
or adding a fallback, is editing that YAML file, not a code change
(docs/architecture/05-technology-choices.md §5.2).

This is a different, complementary mechanism to the `provider_configs`
database table (libs/models/provider.py): this file defines *which
implementations exist* (as importable classes) and the process-wide
default; the DB table tracks a per-channel *active choice* by name among
whatever this file defines, plus usage/cost logging. A caller honoring a
channel's DB override passes that row's `provider_name` as
`preferred_name` below; omitting it uses this file's "active" default.

Provider selection precedence, highest first: `preferred_name` (a
per-call override, e.g. a channel's `provider_configs` row) > an
`{CAPABILITY}_PROVIDER` environment variable (e.g. `VIDEO_GEN_PROVIDER`
for `video_gen`) > this file's `active:` line for the capability. The
env var is an operational convenience for switching a provider (or
overriding it per-environment/deployment) without editing the YAML file
at all — reading it directly here, rather than through a typed
`Settings` field, mirrors this same function's existing `secret_ref`
handling below, and needs no capability-specific code since it's derived
from the capability name alone.

Usage:

    from libs.providers.registry import get_provider

    tts = get_provider("tts")
    audio_bytes = tts.synthesize("hello world")
"""

import importlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from libs.core.config import get_settings

from .base import Provider, ProviderConfigError

# repo root: libs/providers/registry.py -> libs/providers -> libs -> root
_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def _load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = _REPO_ROOT / path
    if not config_path.is_file():
        raise ProviderConfigError(f"providers config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ProviderConfigError(f"providers config file must be a mapping: {config_path}")
    return data


def _instantiate(capability: str, provider_name: str, entry: dict[str, Any]) -> Provider:
    dotted_path = entry.get("class")
    if not dotted_path:
        raise ProviderConfigError(
            f"provider {provider_name!r} for capability {capability!r} is missing 'class'"
        )
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise ProviderConfigError(f"invalid dotted class path: {dotted_path!r}")
    try:
        module = importlib.import_module(module_path)
        provider_class = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ProviderConfigError(
            f"could not import provider class {dotted_path!r} for "
            f"'{capability}.{provider_name}': {exc}"
        ) from exc
    if not (isinstance(provider_class, type) and issubclass(provider_class, Provider)):
        raise ProviderConfigError(
            f"{dotted_path!r} does not subclass libs.providers.base.Provider"
        )

    config = dict(entry.get("config") or {})
    secret_ref = entry.get("secret_ref")
    api_key = os.environ.get(secret_ref) if secret_ref else None

    return provider_class(config=config, api_key=api_key)


def get_provider(capability: str, *, preferred_name: str | None = None) -> Provider:
    """Return the configured provider instance for `capability`
    (`"video_gen"`, `"tts"`, `"image_gen"`, `"stock_media"`,
    `"audio_library"`, `"editor"`, or `"youtube"`).

    `preferred_name`, if given, overrides both the environment variable
    and the config file's `active` default for this one call — the hook
    for honoring a channel-specific `provider_configs` row instead of the
    process-wide default. See the module docstring for the full
    precedence order.
    """
    settings = get_settings()
    config = _load_config(settings.providers_config_path)

    capability_config = config.get(capability)
    if capability_config is None:
        raise ProviderConfigError(f"unknown capability: {capability!r}")

    providers = capability_config.get("providers") or {}
    env_override = os.environ.get(f"{capability.upper()}_PROVIDER")
    provider_name = preferred_name or env_override or capability_config.get("active")
    if not provider_name:
        raise ProviderConfigError(f"capability {capability!r} has no 'active' provider set")

    entry = providers.get(provider_name)
    if entry is None:
        raise ProviderConfigError(
            f"provider {provider_name!r} is not defined for capability {capability!r} "
            f"(available: {sorted(providers)})"
        )

    return _instantiate(capability, provider_name, entry)
