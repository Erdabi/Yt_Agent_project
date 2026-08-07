"""The marker interface every capability-specific provider extends.

`Provider` carries no capability behavior of its own — each real
capability contract (`VideoGenProvider`, `TTSProvider`, `ImageGenProvider`,
`YouTubeProvider`) lives in its own subfolder's `base.py`. This shared
marker exists so `registry.py`'s dynamic import can verify "is this
actually a provider class" before instantiating whatever
`config/providers.yaml` names, rather than blindly calling into an
arbitrary imported class.

Every concrete provider — stub or real — is constructed the same way:
`ProviderClass(config=..., api_key=...)`, so the registry never needs
capability-specific construction logic.

`check_readiness()` is the one behavior this marker does carry, because
"is this capability actually usable right now?" is a question every
capability gets asked and none of them can answer from the outside.
Callers used to approximate it by checking whether the configured class
name started with `Stub` — which quietly conflates *a real provider is
configured* with *that provider's backend can serve a request*. Those
came apart in practice: a real `ComfyUIVideoProvider` pointed at a
ComfyUI instance missing the custom nodes its workflow needs is not a
stub, but every request to it fails. Only the provider itself knows how
to tell the difference, so only the provider can answer.
"""

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderReadiness:
    """Whether a provider can actually serve a request right now, and —
    when it can't — a human-readable reason naming what is missing.
    `reason` is only meaningful when `ready` is False.
    """

    ready: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "ProviderReadiness":
        return cls(ready=True)

    @classmethod
    def unavailable(cls, reason: str) -> "ProviderReadiness":
        return cls(ready=False, reason=reason)


class Provider(ABC):
    def __init__(self, *, config: dict, api_key: str | None) -> None:
        self.config = config
        self.api_key = api_key

    def check_readiness(self) -> ProviderReadiness:
        """Can this provider serve a request right now?

        Defaults to "yes" so that adding a provider never requires
        writing this method: a provider with no cheap way to check
        (nothing to probe short of actually generating) is better off
        answering optimistically and failing honestly at call time than
        claiming an unreadiness it can't substantiate. Override where a
        genuine, cheap check exists — a stub knows it can never serve a
        request; a provider driving a self-hosted backend can ask that
        backend what it supports.

        Implementations must not raise: an unreachable backend is a
        legitimate not-ready answer, not an error for the caller to
        handle.
        """
        return ProviderReadiness.ok()


class StubProvider(Provider):
    """Shared base for every capability's honest placeholder — the
    classes that exist so the config-file switching mechanism is real
    and testable without fabricating a vendor integration.

    A stub is the one provider that can answer `check_readiness()` with
    total certainty: it will never serve a request, whatever the
    environment looks like. Subclasses supply `unavailable_reason` once
    and get both halves of that from here — the readiness answer and the
    `NotImplementedError` their capability method raises — so the
    explanation a caller sees ahead of time and the one it sees on a
    direct call can never drift apart.
    """

    #: Why this capability has no working provider, and what to do about
    #: it. Written as a complete sentence: it is surfaced verbatim both
    #: in readiness reporting and in the raised error.
    unavailable_reason: str = "No real provider is configured for this capability."

    def check_readiness(self) -> ProviderReadiness:
        return ProviderReadiness.unavailable(self.unavailable_reason)

    def _unavailable(self) -> NotImplementedError:
        """Built (not raised) so call sites read `raise
        self._unavailable()` and static analysis still sees the raise.
        """
        return NotImplementedError(self.unavailable_reason)


class ProviderConfigError(RuntimeError):
    """The providers config file is missing, malformed, or names a
    capability/provider that doesn't exist or isn't a real `Provider`.
    """
