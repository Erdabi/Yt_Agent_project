"""The swappable AI/publishing provider layer.

Every capability an agent needs from an external vendor — video
generation, text-to-speech, image generation, YouTube publishing — is
accessed through `get_provider(capability)` (registry.py), never a vendor
SDK directly. Which concrete class backs each capability is declared in
`config/providers.yaml`, so switching Runway for Pika, or ElevenLabs for
Azure Speech, is a config change, not a code change — see registry.py's
docstring for the full picture, including how this relates to the
`provider_configs` database table.
"""

from .base import Provider, ProviderConfigError
from .registry import get_provider

__all__ = ["Provider", "ProviderConfigError", "get_provider"]
