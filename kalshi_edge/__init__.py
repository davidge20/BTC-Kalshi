"""
kalshi_edge

A small research tool to compare:
- a simple short-horizon lognormal probability model (using blended vol)
vs
- Kalshi BTC "ABOVE threshold" binaries (orderbook-derived buy-now prices)

Core concepts:

MarketState:
  "What is BTC right now, and how volatile is it over the time left?"

Ladder evaluation:
  For each strike in the ladder:
    - read orderbook bids
    - derive buy-now proxy prices from reciprocal bids
    - compute model probability of finishing above strike
    - compute buy-only expected value (EV) after fees
"""

__all__ = [
    "constants",
    "http_client",
    "kalshi_api",
    "market_state",
    "market_discovery",
    "ladder_eval",
    "pipeline",
    "render",
    "math_models",
    "formatting",
    "kalshi_auth",
    "trader_v0",
    "trader_v1",
]