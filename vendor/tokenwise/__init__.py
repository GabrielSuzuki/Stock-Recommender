"""tokenwise — an agent wrapper that cuts Claude API spend and proves it.

Four levers, applied to every request:
  routing        send easy work to a small model
  prompt_cache   place cache breakpoints so stable prefixes cost 0.1x
  compaction     stop resending dead weight in long conversations
  semantic_cache skip the API entirely for repeated questions

Every call is written to an append-only ledger with the actual billed cost and
the counterfactual baseline, so savings are audited, not asserted.
"""

from .agent import TokenWiseAgent, text_of
from .caching import CacheOptimizer
from .compaction import Compactor
from .config import Config
from .ledger import Ledger, Record, summarize
from .pricing import PRICES, Price, cost
from .router import Router
from .semantic_cache import SemanticCache

__version__ = "0.1.0"
__all__ = [
    "TokenWiseAgent",
    "Config",
    "Router",
    "CacheOptimizer",
    "Compactor",
    "SemanticCache",
    "Ledger",
    "Record",
    "summarize",
    "cost",
    "PRICES",
    "Price",
    "text_of",
]
