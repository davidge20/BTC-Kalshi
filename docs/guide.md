## `kalshi_edge` guide (run + config)

### What this project is

`kalshi_edge` is a research + trading CLI for **Kalshi BTC Above/Below ladders**. For a given Kalshi event (or an auto-discovered “closing soon” event), it compares ladder prices to a simple, auditable model probability $p_{\text{model}}=\mathbb{P}(S_T \ge K)$ built from deeper BTC venues (Deribit spot/index + Deribit options IV + Coinbase 1-minute realized vol).

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
- `kalshi_edge/trader_engine.py`: trader engine (entry + order lifecycle + JSONL logs)
- `kalshi_edge/backtesting/backtest.py`: backtest CLI entrypoint (`python3 -m kalshi_edge.backtesting.backtest`)
- `kalshi_edge/backtesting/backtest_engine.py`: 1-minute-cadence backtest simulator
- `docs/paper_trading.md`: paper trading (`--dry-run`) and maker-fill simulation notes

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
- `--trade` enables the trader engine (`kalshi_edge/trader_engine.py::Trader`).
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

#### Config key reference (what each field does)

Notes:

- **Prices**: Kalshi binary prices are integer **cents** in $[0,100]$.
- **`edge_pp` / EV**: throughout this repo, “EV” is **dollars per contract** for a $1 binary, computed buy-only and **net of** `FEE_CENTS`.
- **Percent fields**: `BAND_PCT` and `IV_BAND_PCT` are in **percent** units (e.g. `25.0` means ±25%).

##### `strategy` (live evaluation + trading)

| Key | Units / type | What it controls | Used in |
|---|---|---|---|
| `MIN_EV` | float, \$ / contract | Minimum net EV required for a **new entry**. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `ORDER_SIZE` | int, contracts | Contracts added per entry (and per scale-in step), before caps. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_COST_PER_EVENT` | float, \$ | Cap on total entry cost (price + fee) across a single **event**. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_POSITIONS_PER_EVENT` | int, markets | Max number of distinct market tickers held within one event. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_COST_PER_MARKET` | float, \$ | Cap on total entry cost (price + fee) within a single **market ticker**. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_CONTRACTS_PER_MARKET` | int, contracts | Max total contracts held in one market ticker (including scale-ins). | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MIN_TOP_SIZE` | float, contracts | Liquidity gate: require top-of-book size ≥ this value. *(Ignored in backtests.)* | `kalshi_edge/trader_engine.py` |
| `SPREAD_MAX_CENTS` | int, cents | Liquidity gate: skip candidates with spread wider than this. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `DEDUPE_MARKETS` | bool | If true: only one entry per market ticker (and forces `ALLOW_SCALE_IN=false`). | `kalshi_edge/strategy_config.py`, `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `ALLOW_SCALE_IN` | bool | If true: allow adding to an existing market position up to `MAX_CONTRACTS_PER_MARKET`. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `SCALE_IN_COOLDOWN_SECONDS` | int, seconds | Minimum time since last fill before scaling in again. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `SCALE_IN_MIN_EV` | float, \$ / contract | Minimum net EV required for **scale-in** entries (must be ≥ `MIN_EV`). | `kalshi_edge/strategy_config.py`, `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_ENTRIES_PER_TICK` | int | Per evaluation tick, submit at most this many new/amended entries. | `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_STRIKES` | int | Evaluation budget: how many strikes (closest-to-spot) to fetch/score per tick. | `kalshi_edge/run.py`, `kalshi_edge/pipeline.py`, `kalshi_edge/ladder_eval.py` |
| `FEE_CENTS` | int, cents | Flat per-contract fee used in EV and cost math. | `kalshi_edge/ladder_eval.py`, `kalshi_edge/trader_engine.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `ORDER_MODE` | str | Execution mode: `"taker_only"`, `"maker_only"`, or `"hybrid"` (choose best of maker/taker). | `kalshi_edge/trader_engine.py` |
| `POST_ONLY` | bool | Maker safety: avoid posting orders that would cross and become taker fills. | `kalshi_edge/trader_engine.py` |
| `ORDER_REFRESH_SECONDS` | int, seconds | How frequently the trader refreshes tracked orders and throttles amendments. | `kalshi_edge/trader_engine.py` |
| `CANCEL_STALE_SECONDS` | int, seconds | Cancel resting maker orders older than this. | `kalshi_edge/trader_engine.py` |
| `P_REQUOTE_PP` | float, probability points | Cancel/requote resting maker orders when model probability moves by ≥ this amount (absolute $|\Delta p|$). | `kalshi_edge/trader_engine.py` |
| `REFRESH_SECONDS` | int, seconds | Watch-loop sleep between evaluation ticks. | `kalshi_edge/run.py` |
| `WINDOW_MINUTES` | int, minutes | Auto-discovery window for “closing soon” events (only when no `--event/--url`). | `kalshi_edge/market_discovery.py`, `kalshi_edge/run.py` |
| `BAND_PCT` | float, percent | Strike selection band (±%) used when choosing which strikes to evaluate. | `kalshi_edge/run.py`, `kalshi_edge/ladder_eval.py`, `kalshi_edge/backtesting/backtest_engine.py` |
| `SORT_MODE` | str | Ladder sorting: `"ev"` (default), `"strike"`, `"sens"`. | `kalshi_edge/ladder_eval.py`, `kalshi_edge/run.py` |
| `DEPTH_WINDOW_CENTS` | int, cents | Depth diagnostic: sum size within this many cents of best bid (per side). | `kalshi_edge/ladder_eval.py` |
| `THREADS` | int | Thread pool size for concurrent orderbook fetches. | `kalshi_edge/ladder_eval.py`, `kalshi_edge/run.py` |
| `IV_BAND_PCT` | float, percent | Deribit IV input: use options strikes within ± this % of spot when estimating near-ATM IV. | `kalshi_edge/market_state.py`, `kalshi_edge/run.py` |
| `MIN_MINUTES_LEFT` | float, minutes | With `LOCK_EVENT=true`, unlock the current event once time-left ≤ this threshold. | `kalshi_edge/run.py` |
| `LOCK_EVENT` | bool | In watch + auto-discovery: stick to the first discovered event until near-expiry. | `kalshi_edge/run.py` |
| `LOG_SETTLEMENT` | bool | If true (and `--watch --trade`), periodically check/log settlement for positions. | `kalshi_edge/run.py` |
| `TRADE_LOG_DIR` | string path or null | If set (and no `--trade-log-file`), write per-run logs under `TRADE_LOG_DIR/<YYYY-MM-DD>/<run_id>.jsonl`. | `kalshi_edge/run.py` |

##### `strategy.paper` (paper trading / `--dry-run` maker fill simulation)

These only matter when:

- you run with `--dry-run`, and
- `ORDER_MODE` is `maker_only` or `hybrid`, and
- `paper.simulate_maker_fills` is `true`.

| Key | Units / type | What it controls | Used in |
|---|---|---|---|
| `simulate_maker_fills` | bool | Enables synthetic fills for **resting maker** orders. | `kalshi_edge/trader_engine.py`, `kalshi_edge/paper_fill_sim.py`, `docs/paper_trading.md` |
| `tick_seconds` | float, seconds | Rate-limits simulation ticks (0 disables the limiter). | `kalshi_edge/paper_fill_sim.py` |
| `min_top_time_seconds` | float, seconds | Must be “at top” for at least this long before stochastic fills can occur (unless crossing). | `kalshi_edge/paper_fill_sim.py` |
| `fill_prob_per_tick` | float in $[0,1]$ | Per-tick fill probability once eligible. | `kalshi_edge/paper_fill_sim.py` |
| `partial_fill` | bool | If true: fills can be partial. | `kalshi_edge/paper_fill_sim.py` |
| `max_fill_per_tick` | int, contracts | Max contracts filled per simulation tick (when `partial_fill=true`). | `kalshi_edge/paper_fill_sim.py` |
| `slippage_cents` | int, cents | Adversarial slippage applied to simulated fill price (against you). | `kalshi_edge/paper_fill_sim.py` |
| `seed` | int or null | RNG seed for deterministic/reproducible paper fills. | `kalshi_edge/trader_engine.py`, `kalshi_edge/paper_fill_sim.py` |

##### `backtest` (only used by `python3 -m kalshi_edge.backtesting.backtest`)

Note: the backtest harness uses a simplified fill model (“immediate taker fill at ask when a quote exists”) and does **not** have orderbook size, so `strategy.MIN_TOP_SIZE` is ignored.

| Key | Units / type | What it controls | Used in |
|---|---|---|---|
| `SERIES_TICKER` | str | Event series ticker to backtest (default `"KXBTCD"`). | `kalshi_edge/backtesting/backtest_engine.py` |
| `DAYS` | int, days | Rolling lookback window when `START_DATE`/`END_DATE` are not set. | `kalshi_edge/backtesting/backtest.py` |
| `START_DATE` | `YYYY-MM-DD` or null | Fixed start date (UTC day). Must be set together with `END_DATE`. | `kalshi_edge/backtesting/backtest.py` |
| `END_DATE` | `YYYY-MM-DD` or null | Fixed end date (UTC day). Must be set together with `START_DATE`. | `kalshi_edge/backtesting/backtest.py` |
| `EVENTS` | CSV string or null | Optional explicit event tickers (CSV) to simulate instead of listing by date. | `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_EVENTS` | int | Cap number of events simulated. | `kalshi_edge/backtesting/backtest_engine.py` |
| `STEP_MINUTES` | int, minutes | Backtest simulation step size. | `kalshi_edge/backtesting/backtest_engine.py` |
| `MAX_STRIKES` | int | Per step, evaluate up to N strikes. | `kalshi_edge/backtesting/backtest_engine.py` |
| `BAND_PCT` | float, percent | Strike selection band (±%) used by the strike picker. | `kalshi_edge/backtesting/backtest_engine.py` |
| `ONLY_LAST_N_MINUTES` | int minutes or null | If set, only simulate the last N minutes before each event close. | `kalshi_edge/backtesting/backtest_engine.py` |
| `CACHE_DIR` | string path | On-disk gzip JSON cache directory for fetched data. | `kalshi_edge/backtesting/backtest_engine.py` |
| `LOG_DIR` | string path | Directory where backtest JSONL logs are written. | `kalshi_edge/backtesting/backtest.py` |
| `DEBUG_HTTP` | bool | Enable HTTP debug printing in backtests. | `kalshi_edge/backtesting/backtest.py` |

### Logging & outputs

#### Terminal output

The CLI prints a summary (spot/vol/time-left) and a ladder table. The key table columns map directly to code in `kalshi_edge/render.py`:

- `P`: `p_model` = model probability BTC $\ge$ strike at close
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
- `edge_pp` / `ev`: net EV in dollars per contract (see `docs/model.md`)

### Limitations / gotchas (short)

- **Thin books**: if best bids are missing, the system cannot form `Ybuy/Nbuy`, so EV is `None` for that side.
- **Prices are in cents**: all Kalshi prices are integer cents in $[0,100]$.
- **Fees are simplified**: the code uses a flat per-contract `FEE_CENTS` everywhere (not Kalshi’s full fee schedule).
- **Execution model is approximate**:
  - live evaluation uses reciprocal-bid “ask proxies” (`Ybuy/Nbuy`) because asks can be missing/stale
  - backtests use 1-minute candle bid/ask quotes (see `docs/backtest.md`)
- **Liquidity gates matter**: `MIN_TOP_SIZE` and `SPREAD_MAX_CENTS` can eliminate most candidates in thin markets.

