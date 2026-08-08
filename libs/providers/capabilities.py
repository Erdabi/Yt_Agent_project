"""Which script asset types this deployment can actually produce.

Joins the two halves of that question: `ASSET_TYPE_CAPABILITY`
(libs/schemas/script_production.py) says which capability an asset type
needs, and `Provider.check_readiness()` says whether that capability's
configured provider can currently serve a request. Neither half answers
it alone.

This exists because "what can we produce?" was previously answered only
as advice — the CLI wrote a sentence into the Script Agent's prompt
listing the permitted asset types, and nothing anywhere checked the
resulting script against it. A model that overlooks one line of a long
prompt then produces a script that is structurally perfect and
physically impossible, and the contradiction surfaces hours later inside
Asset Generation, after narration and images have already been built.
A capability limit that is only ever stated in a prompt is a suggestion;
this module is what lets it be enforced.
"""

from libs.core.logging import get_logger
from libs.schemas.script_production import ASSET_TYPE_CAPABILITY, AssetType

from .base import ProviderConfigError, ProviderReadiness
from .registry import get_provider

logger = get_logger(__name__)


def capability_readiness(capabilities: set[str] | None = None) -> dict[str, ProviderReadiness]:
    """Ask each capability's *active* provider whether it can serve a
    request right now. Defaults to every capability some asset type
    depends on.

    A capability whose provider cannot even be constructed (missing from
    config, bad import path) is reported unavailable rather than raising:
    callers use this to decide what to plan, and an unconfigured
    capability is a normal deployment state, not an error.
    """
    wanted = capabilities if capabilities is not None else {
        capability for capability in ASSET_TYPE_CAPABILITY.values() if capability
    }
    readiness: dict[str, ProviderReadiness] = {}
    for capability in sorted(wanted):
        try:
            readiness[capability] = get_provider(capability).check_readiness()
        except ProviderConfigError as exc:
            readiness[capability] = ProviderReadiness.unavailable(
                f"no usable provider configured for {capability}: {exc}"
            )
    return readiness


def fulfillable_asset_types(
    readiness: dict[str, ProviderReadiness] | None = None,
) -> set[AssetType]:
    """Every asset type a script may legitimately ask for here.

    Always includes the render-time types (capability `None`) — they
    depend on no provider, so no outage can take them away, and without
    them a script could be left with no legal visual requirement at all.
    """
    resolved = capability_readiness() if readiness is None else readiness
    return {
        asset_type
        for asset_type, capability in ASSET_TYPE_CAPABILITY.items()
        if capability is None or (capability in resolved and resolved[capability].ready)
    }


def unfulfillable_reasons(
    readiness: dict[str, ProviderReadiness] | None = None,
) -> dict[AssetType, str]:
    """The asset types that cannot be produced, each with why — so a
    caller can tell someone *what* to install rather than only that
    something is missing.
    """
    resolved = capability_readiness() if readiness is None else readiness
    reasons: dict[AssetType, str] = {}
    for asset_type, capability in ASSET_TYPE_CAPABILITY.items():
        if capability is None:
            continue
        if capability not in resolved:
            reasons[asset_type] = f"capability {capability!r} was not resolved"
        elif not resolved[capability].ready:
            reasons[asset_type] = resolved[capability].reason
    return reasons
