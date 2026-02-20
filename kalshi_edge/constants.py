"""
constants.py

Global, non-strategy constants.

- **API base URLs** for upstream data sources.
- **Time/math constants** shared across models.

Strategy parameters (EV thresholds, sizing, caps, etc.) intentionally do **not**
live here anymore—see `kalshi_edge.strategy_config`.
"""

# --------------------
# API base URLs
# --------------------

COINBASE: str = "https://api.exchange.coinbase.com"
DERIBIT: str = "https://www.deribit.com/api/v2"
KALSHI: str = "https://api.elections.kalshi.com/trade-api/v2"

# --------------------
# time / math
# --------------------
MINUTES_PER_YEAR: float = 365.0 * 24.0 * 60.0