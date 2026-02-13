# kalshi_edge — BTC “ABOVE ($X)” Kalshi ladder evaluator

A small research CLI that compares **Kalshi BTC “Above/Below” ladder prices** to a **model probability derived from deeper BTC markets** (BTC options/vol + spot). The goal is to identify potential **mispricings** in Kalshi markets that may converge toward the probability implied by more liquid venues.

> **Disclaimer:** This project is for research/education only and is not financial advice. Markets are risky; model error, fees, and liquidity can dominate.

---

## Motivation

BTC options markets (commonly higher liquidity and volume than single Kalshi ladder strikes) are treated here as **more information-dense**: they tend to reflect the market’s view of **short-dated volatility** and tail risk. This project uses that “more accurate” information source to form a probability model for BTC finishing above a strike at a given time, then compares that model to Kalshi prices where mispricings may occur.

Core belief:
- Use **options-implied volatility + spot** to estimate **P(BTC ≥ strike at event close)**.
- Compare that to Kalshi ladder prices.
- When Kalshi deviates, the market may **converge** back toward the model-implied probability as settlement approaches.

---

## What it does (high-level)

Given a currently “closing soon” Kalshi BTC event (hourly-style):
1. Auto-discovers the relevant Kalshi event ticker (e.g., `KXBTCD-...`) **or** accepts a manual event ticker / URL.
2. Loads the associated **“ABOVE ($X)” ladder** (market tickers typically containing `-T`).
3. Pulls orderbooks for those ladder markets.
4. Estimates short-dated volatility from the **BTC options market** + spot.
5. For each strike, computes:
   - **P**: model probability BTC ≥ strike at event close (simple short-dated model)
   - **Liquidity diagnostics**: best bids, “buy-now” proxies, implied spreads, depth
   - **EV**: expected value of buying YES/NO under the model (with a flat per-contract fee assumption)

(If enabled) an optional trading loop can place orders, with a **dry-run** mode that logs actions without submitting live orders.

---

## Project structure (key files)

- `kalshi_edge/run.py`  
  CLI entrypoint. Handles flags, discovery vs manual event selection, watch mode, etc.

- `kalshi_edge/market_discovery.py`  
  Finds the *currently closing-soon* Kalshi BTC event within a time window.

- `kalshi_edge/kalshi_api.py`  
  Fetches event details, extracts the ABOVE ladder markets, and downloads orderbooks.

- `kalshi_edge/market_state.py`  
  Computes spot + volatility inputs used by the model.

- `kalshi_edge/ladder_eval.py`  
  Evaluates each strike: probability, liquidity stats, expected value, ranking.

- `kalshi_edge/pipeline.py`  
  Orchestrator tying discovery → data fetch → model state → ladder evaluation.

- `kalshi_edge/render.py`  
  Prints the output table / formatted view.

- Trading / state / logging (if present in your branch):  
  `kalshi_auth.py`, `trader_v1.py`, `trade_log.py`, etc.

---

# CLI Flags Reference (kalshi_edge)

This document explains the command-line flags supported by the `kalshi_edge` CLI.

---

## CLI flags (what they all mean)

## Event selection

### `--event <TICKER>` (default: `None`)
Manually set the Kalshi **event ticker** (e.g., `KXBTCD-26FEB0518`). If not provided (and `--url` isn’t provided), the program auto-discovers a “closing soon” event.

### `--url <URL>` (default: `None`)
Alternative manual selector: provide a Kalshi **market/event URL**. (Useful if you copied a link instead of a ticker.)

---

## Execution mode

### `--watch` (default: off)
Continuously re-runs evaluation and re-renders output.

### `--refresh-seconds <N>` (default: `10`)
Only used with `--watch`. Sleep time between refresh cycles.

---

## Auto-discovery

### `--window-minutes <N>` (default: `70`)
When auto-discovering, search for an event “closing soon” within the next N minutes.  
If discovery fails, increase this.

---

## Ladder sizing / filtering

### `--max-strikes <N>` (default: `120`)
How many strikes from the ladder to evaluate (after filtering / sorting). Larger = more API calls.

### `--band-pct <PCT>` (default: `25.0`)
Prefer ladder strikes within ±PCT% of spot. Helps focus around the “action” instead of far OTM strikes.

---

## Sorting output

### `--sort {sens,strike,ev}` (default: `ev`)
- `ev`: highest model EV first  
- `strike`: sort by strike level  
- `sens`: sort by sensitivity (closest to 50/50 outcomes first)

---

## EV / liquidity assumptions

### `--fee-cents <C>` (default: `1`)
Flat assumed fee per contract (in cents) used when computing EV. This is a simplification.

### `--depth-window-cents <C>` (default: `2`)
When computing a simple “depth near top-of-book,” look within C cents of best bid.

### `--threads <N>` (default: `10`)
Concurrency for orderbook fetches. Higher may be faster but can stress rate limits.

---

## Volatility estimation

### `--iv-band-pct <PCT>` (default: `3.0`)
For near-ATM IV estimation: consider options within ±PCT% of spot.

---

## Debugging

### `--debug-http` (default: off)
Print HTTP request/response summaries (useful for diagnosing API issues).

---

## Trading flags (only if you enable trading)

Trading is optional. If you do not pass `--trade`, none of the auth/state/trader code is used.

### `--trade` (default: off)
Turn on the trading loop (calls into `trader_v1`).

### `--dry-run` (default: off)
When `--trade` is enabled, do everything except submit real orders. Still logs what it would do.

### `--trade-count <N>` (default: `1`)
Contracts per entry.

### `--max-contracts <N>` (default: `None`)
Cap total position size (in contracts).  
If omitted, defaults to `--trade-count` (i.e., one entry-sized unit max).

### `--min-minutes-left <M>` (default: `2.0`)
Don’t enter new trades if less than M minutes remain until event close (helps avoid late illiquidity).

### `--state-file <PATH>` (default: `.kalshi_edge_state.json` or env override)
Where trader state is stored (positions, etc).  
Can be overridden by env var: `KALSHI_EDGE_STATE_FILE`.

### `--trade-log-file <PATH>` (default: `trade_log.jsonl` or env override)
Append-only JSONL log of trade actions plus a shutdown snapshot (e.g., on Ctrl-C).  
Can be overridden by env var: `KALSHI_EDGE_TRADE_LOG_FILE`.

### `--reconcile-state` (default: off)
On first tick, sync local state with live Kalshi positions for the current event.

---

## Trading auth / connectivity flags (required for `--trade`)

### `--api-key-id <ID>` (default: env `KALSHI_API_KEY_ID`)
Your Kalshi API key id.

### `--private-key-path <PATH>` (default: env `KALSHI_PRIVATE_KEY_PATH`)
Path to your private key file used for signing requests.

### `--kalshi-base-url <URL>` (default: env `KALSHI_BASE_URL` or compiled default)
Base URL for Kalshi API.

If `--trade` is set and `--api-key-id` / `--private-key-path` are missing, the program exits.

## Interpreting the output

### `P`
Model probability that **BTC ≥ strike** at event close.

### `Sens`
Sensitivity score: `p * (1 - p)` (peaks at **0.25** when `p = 0.5`).  
Higher = more sensitive to small changes.

### `Ybid / Nbid`
Best bid (in cents) on **YES** and **NO**.

### `Ybuy / Nbuy` (buy-now proxy)
Approximates the immediate buy price from the reciprocal side:

- `Ybuy ≈ 100 - best NO bid`
- `Nbuy ≈ 100 - best YES bid`

If the reciprocal side is missing, the proxy may be unavailable.

### `SprY / SprN`
Implied spread = **buy-now proxy − best bid** (tighter is better).

### `EV_Y / EV_N`
Expected value (in **$**) of buying **1 contract** under the model after fee.  
Positive EV means “cheap vs model” (not a guarantee; liquidity/fees/slippage matter).

