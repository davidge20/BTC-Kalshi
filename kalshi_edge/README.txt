kalshi_edge — BTC “ABOVE ($X)” Kalshi ladder evaluator
======================================================

Debug:  python3 -m kalshi_edge.run --watch --trade --dry-run --max-strikes 5 --reconcile-state --trade-count 1 --max-contracts 5

What this project does
----------------------
This CLI program finds the *currently closing-soon* Kalshi BTC Above/Below event
(e.g., the hourly “Bitcoin price … at Xpm?” event), loads its “ABOVE ($X)” ladder
(markets with tickers containing “-T”), pulls orderbooks, estimates short-dated
volatility from Deribit + Coinbase, then computes:

- P: model probability BTC >= strike at close (simple lognormal, short-dated)
- Liquidity diagnostics: best bids, “buy-now proxy” prices, implied spreads, depth
- EV: expected value of buying YES/NO (buy-only) under the model after a flat fee

Important:
- This tool is for research/education, not financial advice.
- It currently assumes “buy-only” (we do not place orders; we only evaluate).

Quick start
-----------
From the parent directory (the one that CONTAINS the kalshi_edge folder):

1) Activate your venv if you have one, then run:

  python3 -m kalshi_edge.run

If discovery fails (Kalshi returns no candidates in the closing-soon window),
widen the window:

  python3 -m kalshi_edge.run --window-minutes 360

If you want to see API calls and debug details:

  python3 -m kalshi_edge.run --debug-http

Common run commands
-------------------

A) Single snapshot (default)
  python3 -m kalshi_edge.run

B) Widen discovery window (helpful if no current event is near close)
  python3 -m kalshi_edge.run --window-minutes 1440

C) Sort results by best expected value (default is usually EV)
  python3 -m kalshi_edge.run --sort ev

D) Sort by strike (useful for sanity checks / browsing ladder)
  python3 -m kalshi_edge.run --sort strike

E) Sort by “sensitivity” (contracts closest to p=0.5 first)
  python3 -m kalshi_edge.run --sort sens

F) Increase evaluated strikes (more ladder coverage)
  python3 -m kalshi_edge.run --max-strikes 200

G) Watch mode (refreshes repeatedly)
  python3 -m kalshi_edge.run --watch --refresh-seconds 10

If you have an event ticker and want to bypass discovery
--------------------------------------------------------
If your run.py supports a manual event flag (recommended), use:

  python3 -m kalshi_edge.run --event KXBTCD-26FEB0518

If your current run.py DOES NOT include --event yet, add it (small change):
- Add ap.add_argument("--event", type=str, default=None)
- If args.event is set, skip discovery and use that ticker.

Flags you care about (the important ones)
-----------------------------------------

--watch
  Continuously refresh (instead of a single snapshot).

--refresh-seconds N
  Only used with --watch. How often we re-run the pipeline.

--window-minutes N
  Auto-discovery window for markets “closing soon”.
  If discovery fails, increase this (360 = 6h, 1440 = 24h).

--max-strikes N
  How many ladder strikes to evaluate (selected closest-to-spot first).
  Higher = more API calls + more output.

--band-pct P
  Strike selection prefers strikes within ±P% of spot.
  If there are too few strikes in-band, it falls back to “closest overall”.

--sort {ev,strike,sens}
  ev     = highest model EV first
  strike = lowest strike first
  sens   = closest to p=0.5 first (most “informative” contracts)

--fee-cents C
  Flat fee assumption per contract (in cents) used in EV calculation.
  (This is an approximation; real fees can differ.)

--depth-window-cents D
  Depth is computed as total size within D cents of best bid.
  Useful rough liquidity signal near top-of-book.

--threads N
  Concurrent orderbook fetch threads.
  Higher is faster but can stress the API/network.

--iv-band-pct P
  Used for Deribit implied vol estimate:
  we take near-ATM options within ±P% of spot (then median IV).

--debug-http
  Print all HTTP requests + response summaries (status, bytes, keys).

--trade-count N
  With --trade, contracts to buy per entry (default 1).

--max-contracts N
  With --trade, max total contracts to buy across runtime. Defaults to --trade-count
  (single entry). Uses the state file to persist across runs.

--reconcile-state
  With --trade, sync the local state file against live portfolio positions at startup.

How to interpret key columns
----------------------------

P
  Our model probability BTC >= strike at the event close time.

Sens
  p*(1-p). Peaks at 0.25 when p=0.50.
  High Sens = small price moves meaningfully change probability.

Ybid / Nbid
  Best bids in cents on YES and NO.

Ybuy / Nbuy (buy-now proxy)
  We estimate an “immediate buy” price from the reciprocal book side:
    Ybuy ≈ 100 - best NO bid
    Nbuy ≈ 100 - best YES bid
  (If the reciprocal bid is missing, we can’t infer a buy-now proxy.)

SprY / SprN
  Implied spread (cents) = buy-now-proxy - best bid.
  Smaller is better (tighter market).

EV_Y / EV_N
  Expected value (in $) of buying 1 contract under our model:
    EV = p_win - (price + fee)
  Positive EV means “model thinks it’s underpriced” (not guaranteed).

Troubleshooting
---------------

1) “Could not auto-discover a KXBTCD event…”
   Increase --window-minutes, and run with --debug-http to see samples:
     python3 -m kalshi_edge.run --window-minutes 1440 --debug-http

2) Module import errors:
   Run as a module from the parent directory:
     python3 -m kalshi_edge.run
   NOT:
     python3 kalshi_edge/run.py

3) Thin or empty books:
   Some strikes have missing reciprocal bids, so Ybuy/Nbuy can be None.
   Use more strikes or a wider band, or focus near spot.

Project structure (high-level)
------------------------------
kalshi_edge/
  market_discovery.py   -> auto-discover current KXBTCD event (closing soon)
  kalshi_api.py         -> fetch event, extract ABOVE markets, fetch orderbooks
  market_state.py       -> spot + implied vol + realized vol -> blended vol
  ladder_eval.py        -> compute P / liquidity / EV for ladder markets
  render.py             -> prints the summary + ladder table
  http_client.py        -> tiny HTTP wrapper with debug toggle
  math_models.py        -> probability + helpers
  constants.py          -> base URLs and shared constants

Notes / limitations
-------------------
- This tool currently evaluates only (no order placement).
- Probability model is intentionally simple; short-dated crypto is noisy.
- Fees are modeled as a flat number of cents; adjust as needed.
