"""Diagnostics for the market-data layer.

    python -m mcp.market_data            # check the universe fetch

A package __main__ rather than a __main__ block inside universe.py: running
`python -m mcp.market_data.universe` imports the module twice (once as part of
the package, once as __main__) and Python warns about the unpredictable
behaviour that can cause.
"""

import sys

from .universe import _main

if __name__ == "__main__":
    sys.exit(_main())
