## Backtesting (1-minute candlesticks, fast iteration)

This repo includes a minute-cadence backtest harness designed for **fast strategy iteration** using historical 1-minute candles (quotes) rather than full historical orderbooks.

Canonical entrypoint:

```bash
python3 -m kalshi_edge.backtest
```

### What we simulate (as implemented)

In `kalshi_edge/backtest_engine.py` the simulator runs a loop at `backtest.STEP_MINUTES` cadence (typically 1):

- **Quote source**: Kalshi 1-minute candlesticks per market (YES bid/ask in cents when available)
- **Decision rule**: compute `p_model` at each minute and select EV-positive candidates
- **Fills**: immediate **taker-style fills at the ask** when an ask quote exists
- **Fees**: flat `strategy.FEE_CENTS` per contract
- **Position management**: uses the same caps/filters as live config where applicable:
  - `MIN_EV`, `SPREAD_MAX_CENTS`, `DEDUPE_MARKETS`, `ALLOW_SCALE_IN`, caps, etc.
- **Settlement**: hold to expiry and settle to \$1/\$0 using the market’s final outcome (best-effort parsed)

What we do **not** simulate:

- orderbook depth / size (no L2 snapshots)
- maker queue dynamics, partial fills, intraminute microstructure

As a result, **`strategy.MIN_TOP_SIZE` is ignored** in backtests (candles don’t include top size).

### Data sources

- **Kalshi 1-minute candlesticks**
  - Live-tier endpoint: `/markets/<ticker>/candlesticks` (and best-effort batch variants)
  - Historical-tier endpoint: `/historical/markets/<ticker>/candlesticks`
  - The harness uses `/historical/cutoff` to decide which tier to query for a given market close time.
- **Coinbase BTC-USD 1-minute candles**
  - Used to compute spot and realized volatility in backtests.

### How to run

Backtests are configured via the same JSON config file used for live runs.

```bash
export KALSHI_EDGE_CONFIG_JSON=/path/to/config.json
python3 -m kalshi_edge.backtest
```

Optional config override:

```bash
python3 -m kalshi_edge.backtest --config /path/to/config.json
```

There are no tuning CLI flags beyond `--config`; tuning lives under the `"backtest"` section (and shared `"strategy"` knobs still apply).

### Backtest config keys (`"backtest": {...}`)

All keys below are defined in `kalshi_edge/strategy_config.py::BacktestConfig`.

- **Universe / time range**
  - `SERIES_TICKER`: series to pull settled events from (default `"KXBTCD"`)
  - `DAYS`: rolling lookback window (used when `START_DATE`/`END_DATE` are not set)
  - `START_DATE`, `END_DATE`: optional explicit range (`YYYY-MM-DD`), inclusive of both endpoints by day
  - `EVENTS`: optional CSV list of specific event tickers to backtest (skips listing by date)
  - `MAX_EVENTS`: cap number of events to simulate
- **Cadence / selection**
  - `STEP_MINUTES`: simulation step size (minutes)
  - `MAX_STRIKES`: evaluate up to N strikes per minute (closest-to-spot, with a band heuristic)
  - `BAND_PCT`: strike band around spot (percent) used by the strike picker
  - `ONLY_LAST_N_MINUTES`: if set, only simulate the last N minutes before each event close
- **Storage / debugging**
  - `CACHE_DIR`: on-disk gzip JSON cache directory
  - `LOG_DIR`: directory where JSONL backtest logs are written
  - `DEBUG_HTTP`: enable HTTP debug printing

### Caching

The backtest engine uses an on-disk gzip JSON cache (`kalshi_edge/cache.py::FileCache`) under `backtest.CACHE_DIR`, including:

- `kalshi_markets/<EVENT>.json.gz`
- `kalshi_candles/<MARKET_TICKER>/<start>-<end>-1m.json.gz`
- `coinbase/BTC-USD/<start>-<end>-1m.json.gz`

To wipe cache, delete the configured cache directory:

```bash
rm -rf data/cache
```

(If you changed `CACHE_DIR`, delete that path instead.)

### Output

#### Console summary

`python3 -m kalshi_edge.backtest` prints a summary from `kalshi_edge/backtest_report.py`, including:

- events scanned / simulated
- trades + contracts
- total PnL and PnL per trade
- win rate
- top events by PnL
- JSONL log path

#### JSONL log file

Each run writes a JSONL log under `backtest.LOG_DIR` named like:

`backtest_<SERIES_TICKER>_<YYYYMMDD>_<YYYYMMDD>_<timestamp>.jsonl`

Record types you’ll see in the file:

- `record_type="entry"`: one line per simulated fill (minute timestamp, side, price, `p_yes`, `ev`, etc.)
- `record_type="event_summary"`: per-event trades/contracts/PnL/win_rate
- `record_type="run_summary"`: run-wide totals + an embedded copy of the effective config

### Known limitations / next steps

- **Minute granularity**: intraminute moves and fills are not modeled.
- **Candles are not orderbooks**: quote availability can be spotty; missing bid/ask reduces tradable minutes.
- **Fill model is simplified**: immediate taker fill at ask (no slippage, no depth-walk).
- **Volatility is realized-only**: no Deribit IV input in backtests today.

