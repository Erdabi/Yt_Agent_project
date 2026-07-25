"""The marker interface every capability-specific provider extends.

`Provider` carries no capability behavior of its own — each real
capability contract (`VideoGenProvider`, `TTSProvider`, `ImageGenProvider`,
`YouTubePublisher`) lives in its own subfolder's `base.py`. This shared
marker exists so `registry.py`'s dynamic import can verify "is this
actually a provider class" before instantiating whatever
`config/providers.yaml` names, rather than blindly calling into an
arbitrary imported class.

Every concrete provider — stub or real — is constructed the same way:
`ProviderClass(config=..., api_key=...)`, so the registry never needs
capability-specific construction logic.
"""

from abc import ABC


class Provider(ABC):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        self.config = config
        self.api_key = api_key


class ProviderConfigError(RuntimeError):
    """The providers config file is missing, malformed, or names a
    capability/provider that doesn't exist or isn't a real `Provider`.
    """
