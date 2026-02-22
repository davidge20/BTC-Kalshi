# `kalshi_edge`

Research + trading CLI for **Kalshi BTC Above/Below ladders**. It compares ladder prices (YES/NO binaries, quoted in **cents**) to a simple model probability \(p_{\text{model}}=\mathbb{P}(S_T \ge K)\) derived from deeper BTC venues (spot + volatility), and reports **execution-aware EV** per contract.

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

- `kalshi_edge/docs/guide.md`: how to run the project + how config works
- `kalshi_edge/docs/model.md`: probability + EV model (`p_model`, `sigma_blend`, `edge_pp`)
- `kalshi_edge/docs/backtest.md`: minute-cadence backtesting with 1-minute candlesticks
