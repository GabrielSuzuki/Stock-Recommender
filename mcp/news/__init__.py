"""News, earnings dates and SEC filings for the catalyst check.

Same seam principle as market_data: everything upstream calls `get_context()`
and never a vendor SDK.
"""

from .disqualify import DISQUALIFY_RULES, check_disqualifiers
from .providers import (EdgarProvider, FinnhubProvider, NewsContext,
                        OfflineNewsProvider, get_context, resolve_news_provider)

__all__ = ["get_context", "NewsContext", "FinnhubProvider", "EdgarProvider",
           "OfflineNewsProvider", "resolve_news_provider",
           "check_disqualifiers", "DISQUALIFY_RULES"]
