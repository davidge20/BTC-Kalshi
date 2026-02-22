# Backtest Harness (1-Minute Candles)

Run backtests with config-only tuning (no tuning flags on CLI).

## Run

```bash
export KALSHI_EDGE_CONFIG_JSON=/path/to/strategy_config.json
python3 -m kalshi_edge.backtest
```

Optional config override:

```bash
python3 -m kalshi_edge.backtest --config /path/to/strategy_config.json
```

## Config Layout

Use one JSON file with two top-level sections:

- `strategy`: live/shared strategy params (`MIN_EV`, caps, sizing, fee, etc.)
- `backtest`: backtest-only params (`DAYS`, date range, cache/log dirs, series, etc.)

Backwards compatibility:

- legacy flat strategy JSON is still supported by `load_config()`
- legacy backtest flat keys are supported as `BT_<FIELD>`

## What Is Simulated

- 1-minute cadence decision loop
- Kalshi market candles as quote source
- YES/NO EV checks using existing model conventions
- immediate taker fills at ask when quote exists
- hold to expiration, then settle PnL by market outcome

## What Is Not Simulated

- orderbook depth / top size (no L2 snapshots in this harness)
- maker queue dynamics or intraminute microstructure

`MIN_TOP_SIZE` is explicitly ignored in backtest mode.

## Tuning Workflow

Edit the JSON under `"backtest"` and rerun:

- date range: `START_DATE` + `END_DATE` or rolling `DAYS`
- universe: `SERIES_TICKER`, `EVENTS`, `MAX_EVENTS`
- speed/selection: `STEP_MINUTES`, `MAX_STRIKES`, `BAND_PCT`, `ONLY_LAST_N_MINUTES`
- storage: `CACHE_DIR`, `LOG_DIR`

After first run populates cache, reruns should mostly be CPU-bound.
