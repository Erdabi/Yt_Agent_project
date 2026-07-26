"""The only `stock_media` provider configured today (see
config/providers.yaml). Exists so the config-file switching mechanism
itself is fully real and testable (libs/providers/registry.py), without
fabricating an actual stock-footage vendor integration, which is
deferred to Phase 1 (docs/architecture/06-roadmap.md).
"""

from typing import Any

from .base import StockMediaProvider


class StubStockMediaProvider(StockMediaProvider):
    def search(self, query: str, **kwargs: Any) -> bytes:
        raise NotImplementedError(
            "No real stock media provider is configured. Add one under "
            "libs/providers/stock_media/, register it in "
            "config/providers.yaml, and set stock_media.active to its "
            "name (docs/architecture/06-roadmap.md, Phase 1)."
        )
