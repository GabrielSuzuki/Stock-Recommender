"""Market data: one seam between the pipeline and whoever is selling bars today.

Provider churn is the highest-maintenance part of a system like this -- APIs
get deprecated, free tiers shrink, endpoints move. Everything upstream of this
package talks to `get_bars()` and never to a vendor SDK, so replacing Alpaca
costs one file.

    from mcp.market_data import get_bars, load_universe

    symbols = load_universe()
    bundle = get_bars(symbols, lookback_days=500)
    bars = bundle.bars          # dict[str, DataFrame]
    print(bundle.stats)         # what was fetched, what failed
"""

from .cache import BarCache
from .provider import (AlpacaProvider, BarBundle, OfflineProvider, Provider,
                       StaleDataError, get_bars, resolve_provider)
from .universe import load_universe

__all__ = ["BarCache", "Provider", "AlpacaProvider", "OfflineProvider",
           "BarBundle", "StaleDataError", "get_bars", "resolve_provider",
           "load_universe"]
