"""
kalshi_edge

A small research tool to compare:
- a simple short-horizon lognormal probability model (using blended vol)
vs
- Kalshi BTC "ABOVE threshold" binaries (orderbook-derived buy-now proxy prices)

This package is built to answer one question:
  "Are Kalshi ladder prices consistent with probability implied by deeper BTC markets
   (spot + options-implied volatility), net of fees and realistic execution?"

Core concepts
-------------

Kalshi contract semantics:
- "ABOVE strike" event settles to 1 if BTC settlement price $\ge$ strike at expiry, else 0.
- Each market has YES and NO shares (complements, but not perfectly due to spread/fees).
- Our model produces P(ABOVE). Strategy compares that probability to executable prices
  on each side (YES or NO) to identify potential mispricings.

MarketState:
  "What is BTC right now, how volatile is it, and how much time is left until expiry?"

Model choice:
- We use a lognormal/GBM-style approximation for short horizons because it gives a fast,
  explainable baseline probability anchored to liquid BTC spot/vol markets.
- "Blended vol" is used to stabilize the estimate vs single-venue noise (single prints,
  stale quotes, or venue-specific quirks).

Executable price approximation ("buy-now proxy"):
- Kalshi books are often thin; best-ask can be missing, stale, or non-representative.
- We estimate a conservative buy price using the opposite-side best bid complement:
    YES_buy ≈ 100 - best_bid(NO)
    NO_buy  ≈ 100 - best_bid(YES)
  (in cents, before fees)
- This is a proxy: complements do not sum to 100 exactly once spread + fees are considered.
  Treat this as "can I likely get filled near here right now?" rather than a theoretical price.

In more detail, Kalshi ladder order books are often thin and asymmetric, 
so the “best ask” needed to price an immediate buy can be missing, stale, or based on 
tiny, non-representative size. To avoid breaking EV/edge calculations 
(or assuming unrealistic fills), we estimate an executable “buy-now” proxy using 
the opposite side's best bid and the YES/NO complement relationship for binaries: 
in cents, YES_buy ≈ 100 - best_bid(NO) and NO_buy ≈ 100 - best_bid(YES). 
This uses the most reliable live signal (active bids) to infer a conservative 
buy price even when direct asks are unavailable. Because spreads, discrete 
pricing, and fees mean complements won't sum to exactly 100 in practice, 
this proxy should be treated as an execution-aware approximation 
(“could I likely get filled near here now?”) rather than a frictionless 
theoretical price.

Edge / EV conventions:
- edge_pp = model_probability - implied_probability_from_executable_price
- EV is computed for buy-only entry (taker-style), net of Kalshi fees, per 1 contract.
- We intentionally ignore maker fills and perfect exits to avoid overstating achievable edge.

Code map
--------
- market_discovery.py: find the relevant Kalshi event (e.g., closing soon)
- kalshi_api/http_client/kalshi_auth: authenticated API access + HTTP utilities
- market_state.py: compute "time-left + spot + vol" inputs for the model
- math_models.py: probability model(s)
- ladder_eval.py: apply model vs each strike + compute EV/edge
- pipeline.py: orchestrates discovery -> fetch -> evaluate
- render.py: formatting / table output
- trader_v0/v1.py: (optional) automated entry/exit loop(s)
- trade_log.py: durable event/trade logging
"""

__all__ = [
    "constants",
    "formatting",
    "http_client",
    "kalshi_api",
    "kalshi_auth",
    "ladder_eval",
    "market_discovery",
    "market_state",
    "math_models",
    "pipeline",
    "render",
    "run",
    "trade_log",
    "trader_v0",
    "trader_v1",
]
