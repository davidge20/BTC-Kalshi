"""
kalshi_edge

A research + trading CLI that evaluates Kalshi BTC "Above/Below" ladder markets
against a model probability derived from deeper BTC venues (spot + options IV /
realized vol).

Core concepts
-------------

Kalshi contract semantics:
- "ABOVE strike" event settles to 1 if BTC settlement price >= strike at expiry, else 0.
- Each market has YES and NO shares (complements, but not perfectly due to spread/fees).
- Our model produces P(ABOVE). Strategy compares that probability to executable prices
  on each side (YES or NO) to identify potential mispricings.

MarketState:
  "What is BTC right now, how volatile is it, and how much time is left until expiry?"

Model choice:
- Lognormal/GBM-style approximation for short horizons — fast, explainable baseline
  anchored to liquid BTC spot/vol markets.
- "Blended vol" stabilizes the estimate vs single-venue noise.

Executable price approximation ("buy-now proxy"):
- Kalshi books are often thin; best-ask can be missing, stale, or non-representative.
- We estimate a conservative buy price using the opposite-side best bid complement:
    YES_buy ≈ 100 - best_bid(NO)
    NO_buy  ≈ 100 - best_bid(YES)
  (in cents, before fees)
- This is an execution-aware approximation, not a frictionless theoretical price.

Edge / EV conventions:
- edge_pp = model_probability - implied_probability_from_executable_price
- EV is computed for buy-only entry (taker-style), net of Kalshi fees, per 1 contract.

Package layout
--------------
Top-level modules:
  run.py              CLI entrypoint (also accessible via __main__.py)
  strategy_config.py  StrategyConfig dataclass + JSON loader
  pipeline.py         Orchestrates discovery -> fetch -> evaluate
  market_discovery.py Find the relevant Kalshi event (e.g., closing soon)
  market_state.py     Compute "time-left + spot + vol" inputs for the model
  math_models.py      Probability model(s)
  ladder_eval.py      Apply model vs each strike + compute EV/edge
  render.py           Terminal table output
  trader_engine.py    Canonical trading engine (Trader)
  order_manager.py    Single-order lifecycle (create/amend/cancel/fill tracking)
  trade_log.py        Append-only JSONL event logger
  (backtesting/)      Backtesting harness (CLI + engine + caches)

Sub-packages:
  data/kalshi/        Kalshi API client (client.py) + market-data parsing (models.py)
  util/               Shared helpers — time.py (timestamps), coerce.py (safe casts)
  telemetry/          State I/O (state_io.py)
  report/             Post-run analysis (analyze.py — settlement PnL)
"""

__all__ = [
    "constants",
    "http_client",
    "kalshi_auth",
    "ladder_eval",
    "market_discovery",
    "market_state",
    "math_models",
    "pipeline",
    "render",
    "run",
    "backtesting",
    "trade_log",
    "trader_engine",
    # sub-packages
    "data",
    "util",
    "telemetry",
    "report",
]
