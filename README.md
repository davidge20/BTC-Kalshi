# `kalshi_edge`

Research + trading CLI for **Kalshi BTC Above/Below ladders**. It compares ladder prices (YES/NO binaries, quoted in **cents**) to a simple model probability $p_{\text{model}}=\mathbb{P}(S_T \ge K)$ derived from deeper BTC venues (spot + volatility), and reports **execution-aware EV** per contract.

> Disclaimer: research/education only — not financial advice. These markets are risky: model error, fees, and liquidity can dominate.

## Quick start

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

# run a single evaluation snapshot
python3 -m kalshi_edge --config strategy_config.example.json

# watch mode
python3 -m kalshi_edge --config strategy_config.example.json --watch

# paper trader (no real orders)
python3 -m kalshi_edge --config strategy_config.example.json --watch --trade --dry-run
```

## Docs

- `docs/guide.md`: how to run the project + how config works
- `docs/model.md`: probability + EV model (`p_model`, `sigma_blend`, `edge_pp`)
- `docs/paper_trading.md`: paper trading (`--dry-run`) and maker-fill simulation notes
- `docs/backtest.md`: minute-cadence backtesting with 1-minute candlesticks

## Dashboard (local Streamlit)

This repo includes an **additive, local-only** Streamlit dashboard under `dashboard/` that can visualize:

- market/model edge snapshots (ladder curves, EV, break-even)
- orders + fills (best-effort from JSONL logs)
- portfolio/risk (best-effort from fills)
- performance (PnL, simple calibration proxy, trade journal)
- system health (data freshness, latest errors, config metadata)

### Quickstart

```bash
python3 -m pip install -r requirements.txt
streamlit run dashboard/app.py
```

### Ingest existing JSONL logs into SQLite

Example using the repo’s bundled dry-run logs:

```bash
python3 -m dashboard.ingest.ingest_jsonl --input logs/dryrun_trades.jsonl --db .dashboard/kalshi_edge_dashboard.sqlite
python3 -m dashboard.ingest.ingest_jsonl --input logs/v2_dryrun.jsonl --db .dashboard/kalshi_edge_dashboard.sqlite
streamlit run dashboard/app.py
```

### Ingest backtest results (historical)

Run a backtest to produce a `backtests/backtest_*.jsonl`, then ingest it:

```bash
python3 -m kalshi_edge.backtesting.backtest --config strategy_config.example.json
python3 -m dashboard.ingest.ingest_backtest_jsonl --input backtests/backtest_*.jsonl --db .dashboard/kalshi_edge_dashboard.sqlite
streamlit run dashboard/app.py
```

### Optional: live tail ingestion

```bash
python3 -m dashboard.ingest.ingest_live --input /path/to/trade_log.jsonl --db .dashboard/kalshi_edge_dashboard.sqlite
```

### Control Center (run from the app)

The dashboard includes a **Control Center** tab that can:

- load/edit the same JSON config (`strategy` + `backtest`)
- run backtests (and auto-ingest results)
- start evaluation / paper trading / live trading (runs the existing CLI under the hood)

Run:

```bash
streamlit run dashboard/app.py
```
