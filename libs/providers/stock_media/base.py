"""Stock media provider interface.

A concrete implementation searches a stock footage/photo library for a
query and returns the best match, used by the Video Agent's Asset
Generation module (services/agent_video/app/modules/asset_generation.py)
for `stock_footage`-type asset requirements. No real implementation
exists yet — see stub_provider.py and docs/architecture/06-roadmap.md,
Phase 1.
"""

from abc import abstractmethod
from typing import Any

from libs.providers.base import Provider


class StockMediaProvider(Provider):
    @abstractmethod
    def search(self, query: str, **kwargs: Any) -> bytes:
        """Search stock media for `query` and return the best match's raw
        bytes.
        """
