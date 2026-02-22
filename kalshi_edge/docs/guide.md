## `kalshi_edge` guide (run + config)

### What this project is

`kalshi_edge` is a research + trading CLI for **Kalshi BTC Above/Below ladders**. For a given Kalshi event (or an auto-discovered “closing soon” event), it compares ladder prices to a simple, auditable model probability \(p_{\text{model}}=\mathbb{P}(S_T \ge K)\) built from deeper BTC venues (Deribit spot/index + Deribit options IV + Coinbase 1-minute realized vol).

The output is a terminal table of **probability**, **liquidity/execution diagnostics**, and **per-contract EV** (net of a simplified flat fee) for each strike. Optional trading mode uses the same evaluation output to place orders under configurable caps and filters.

### Repo structure (key modules)

- `kalshi_edge/run.py`: CLI entrypoint (also `python3 -m kalshi_edge` via `kalshi_edge/__main__.py`)
- `kalshi_edge/strategy_config.py`: config dataclasses + JSON loader (`KALSHI_EDGE_CONFIG_JSON`)
- `kalshi_edge/pipeline.py`: orchestration (`evaluate_event`)
- `kalshi_edge/market_discovery.py`: “closing soon” event discovery (`KXBTCD-...`)
- `kalshi_edge/market_state.py`: spot/vol inputs + `sigma_blend`
- `kalshi_edge/math_models.py`: probability model (`lognormal_prob_above`)
- `kalshi_edge/ladder_eval.py`: orderbook parsing, `p_model`, `EV`, “buy-now proxy”
- `kalshi_edge/render.py`: terminal table output
- `kalshi_edge/trader_v2_engine.py`: trading engine (V2; entry + order lifecycle + JSONL logs)
- `kalshi_edge/backtest.py`: backtest CLI entrypoint (`python3 -m kalshi_edge.backtest`)
- `kalshi_edge/backtest_engine.py`: 1-minute-cadence backtest simulator

### Setup

From the repo root (the folder that contains `kalshi_edge/`):

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Dependencies are intentionally minimal (see `requirements.txt`).

### Environment variables (and required auth for live trading)

- **Config**
  - `KALSHI_EDGE_CONFIG_JSON`: path to your JSON config file (see next section)
- **Kalshi auth (required for live trading; not required for `--dry-run`)**
  - `KALSHI_API_KEY_ID`
  - `KALSHI_PRIVATE_KEY_PATH`
  - Optional: `KALSHI_BASE_URL` (defaults to `kalshi_edge/constants.py::KALSHI`)
- **Optional paths**
  - `KALSHI_EDGE_STATE_FILE`: default `.kalshi_edge_state.json`
  - `KALSHI_EDGE_TRADE_LOG_FILE`: default `trade_log.jsonl`

### Running the tool (live evaluation + optional trader)

#### Evaluate once (single snapshot)

```bash
python3 -m kalshi_edge --config /path/to/config.json
```

Target selection (optional):

```bash
python3 -m kalshi_edge --config /path/to/config.json --event "KXBTCD-25FEB2108"
python3 -m kalshi_edge --config /path/to/config.json --url "https://kalshi.com/markets/..."
```

#### Watch mode (refresh loop)

```bash
python3 -m kalshi_edge --config /path/to/config.json --watch
```

The loop sleeps `strategy.REFRESH_SECONDS` between ticks.

#### Trader mode (paper vs live)

Paper trading (no real orders):

```bash
python3 -m kalshi_edge --config /path/to/config.json --watch --trade --dry-run
```

Live trading:

```bash
export KALSHI_API_KEY_ID="..."
export KALSHI_PRIVATE_KEY_PATH="/path/to/private-key.pem"
python3 -m kalshi_edge --config /path/to/config.json --watch --trade
```

Notes:
- `--trade` enables the V2 engine (`kalshi_edge/trader_v2_engine.py::V2Trader`).
- If you pass `--reconcile-state`, the trader will attempt a best-effort startup reconciliation against live positions.

### Config workflow (VERY IMPORTANT)

#### Canonical JSON structure

This repo expects **one** JSON file with up to two top-level sections:

```json
{
  "strategy": { "...": "live + shared knobs" },
  "backtest": { "...": "backtest-only knobs" }
}
```

#### How it’s loaded (`strategy_config.py`)

- **Config path selection**:
  - Preferred: `--config /path/to/config.json` (sets `KALSHI_EDGE_CONFIG_JSON` for the process)
  - Alternative: `export KALSHI_EDGE_CONFIG_JSON=/path/to/config.json`
- **Backwards compatibility**:
  - If there is **no** top-level `"strategy"` object, `load_config()` treats the top-level object as “legacy flat strategy keys”.
  - If there is **no** top-level `"backtest"` object, `load_backtest_config()` reads legacy backtest keys with a `BT_` prefix (e.g. `BT_DAYS`).
- **Unknown keys** are ignored with a warning.

#### Minimal config example

This is intentionally small; everything omitted falls back to defaults in `StrategyConfig` / `BacktestConfig`.

```json
{
  "strategy": {
    "MIN_EV": 0.05,
    "ORDER_SIZE": 1,
    "MAX_COST_PER_EVENT": 5.0,
    "MAX_COST_PER_MARKET": 2.0,
    "MAX_POSITIONS_PER_EVENT": 5,
    "MAX_CONTRACTS_PER_MARKET": 2,
    "MIN_TOP_SIZE": 1.0,
    "SPREAD_MAX_CENTS": 30,
    "DEDUPE_MARKETS": true,
    "ALLOW_SCALE_IN": false,
    "MAX_ENTRIES_PER_TICK": 1,
    "MAX_STRIKES": 10,
    "FEE_CENTS": 1,
    "ORDER_MODE": "taker_only",
    "REFRESH_SECONDS": 10,
    "WINDOW_MINUTES": 70,
    "BAND_PCT": 25.0,
    "SORT_MODE": "ev",
    "TRADE_LOG_DIR": "logs/raw"
  },
  "backtest": {
    "SERIES_TICKER": "KXBTCD",
    "DAYS": 14,
    "MAX_EVENTS": 50,
    "STEP_MINUTES": 1,
    "MAX_STRIKES": 120,
    "BAND_PCT": 25.0,
    "CACHE_DIR": "data/cache",
    "LOG_DIR": "backtests"
  }
}
```

For a complete example that includes every current key, see `strategy_config.example.json`.

### Logging & outputs

#### Terminal output

The CLI prints a summary (spot/vol/time-left) and a ladder table. The key table columns map directly to code in `kalshi_edge/render.py`:

- `P`: `p_model` = model probability BTC \(\ge\) strike at close
- `Ybid/Nbid`: best bids (cents) from Kalshi orderbook
- `Ybuy/Nbuy`: **buy-now proxy** prices (cents), derived as:
  - `Ybuy ≈ 100 - Nbid`
  - `Nbuy ≈ 100 - Ybid`
- `EV_Y/EV_N`: buy-only EV in **dollars per contract**, net of `FEE_CENTS`

#### JSONL trade logs (when `--trade` is enabled)

Trading mode writes an append-only JSONL log via `kalshi_edge/trade_log.py::TradeLogger`.

- **Default path**: `trade_log.jsonl` (current working directory)
- **Organized per-run logs**: set `strategy.TRADE_LOG_DIR`, which writes:
  - trade log: `<TRADE_LOG_DIR>/<YYYY-MM-DD>/<run_id>.jsonl`
  - config snapshot: `<same dir>/<run_id>.config.json`

State path defaults to `.kalshi_edge_state.json`, or can be overridden by `--state-file` / `KALSHI_EDGE_STATE_FILE`.

Core fields you’ll commonly see in the JSONL:

- `ts_utc`: UTC timestamp (ISO8601)
- `event`: event name (e.g. `tick_summary`, `candidate`, `decision`, `order_submit`, `entry_filled`)
- `price_cents`: candidate/order price in cents
- `fee_cents`: assumed fee in cents per contract
- `p_yes` / `p_win`: model probability for YES / side-specific win probability
- `edge_pp` / `ev`: net EV in dollars per contract (see `kalshi_edge/docs/model.md`)

### Limitations / gotchas (short)

- **Thin books**: if best bids are missing, the system cannot form `Ybuy/Nbuy`, so EV is `None` for that side.
- **Prices are in cents**: all Kalshi prices are integer cents in \([0,100]\).
- **Fees are simplified**: the code uses a flat per-contract `FEE_CENTS` everywhere (not Kalshi’s full fee schedule).
- **Execution model is approximate**:
  - live evaluation uses reciprocal-bid “ask proxies” (`Ybuy/Nbuy`) because asks can be missing/stale
  - backtests use 1-minute candle bid/ask quotes (see `kalshi_edge/docs/backtest.md`)
- **Liquidity gates matter**: `MIN_TOP_SIZE` and `SPREAD_MAX_CENTS` can eliminate most candidates in thin markets.

