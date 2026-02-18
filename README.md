# `kalshi_edge`: research CLI for Kalshi BTC “ABOVE ($K$)” ladders

`kalshi_edge` is a small research-oriented CLI that compares **Kalshi BTC threshold ladder prices** (YES/NO binaries) to a **model-implied probability** derived from deeper BTC venues (spot + volatility). The output is an “execution-aware” table of **probability, liquidity diagnostics, and per-contract expected value (EV)** for each strike.

> **Disclaimer:** Research/education only — not financial advice. These markets are risky: model error, fees, and liquidity can dominate.

---

## What problem this is solving

Kalshi’s BTC “Above/Below” events list many strikes (a *ladder*). Individual strikes can be thin, stale, or asymmetric, so the displayed prices can deviate from what you’d expect if you price the event off more liquid BTC markets.

This repo answers:
- Given time remaining until close, what’s a reasonable probability estimate for $S_T \ge K$?
- Given today’s orderbook, what is a conservative “buy now” entry price proxy?
- Net of a simplified fee, is buying YES or NO **positive EV** under the model?

For the implementation-traced report (formulas + conventions tied directly to code), start at `kalshi_edge/docs/README.md`.

---

## How it works (high level)

For a “closing soon” Kalshi BTC event (auto-discovered) or a user-supplied event:
- Pull the “ABOVE ($K$)” ladder markets (strike $K$ in USD) and their orderbooks.
- Build a market state from deeper venues:
  - spot $S_0$ from a Deribit index price
  - implied vol from Deribit options (near-ATM band)
  - realized vol from Coinbase 1-minute candles (1h window)
  - a simple blend rule for $\sigma_{\text{blend}}$
- For each strike $K$:
  - compute $p_{\text{model}} = \mathbb{P}(S_T \ge K)$
  - compute execution/liquidity diagnostics (bids, proxy spreads, depth near top-of-book)
  - compute buy-only EV for YES and/or NO (when the proxy price is available)

---

## A tiny bit of the math (kept readable)

- **Probability model (short-dated lognormal baseline)**:
  - The code assumes $\ln(S_T/S_0)\sim\mathcal{N}(0,\sigma^2 t)$, where $t$ is time-to-close in years and $\sigma$ is annualized volatility.
  - Then:

    $$
    p_{\text{model}} = 1 - \Phi\!\left(\frac{\ln(K/S_0)}{\sigma\sqrt{t}}\right)
    $$

  - See the full implementation-traced derivation in `kalshi_edge/docs/model.md`.

- **EV (expected value) per contract (dollars)**:
  - A Kalshi binary settles to \$1 if it wins, else \$0.
  - Using a conservative “buy-now proxy” price $c$ (in cents) and a simplified constant fee `fee_cents`:

    $$
    \mathrm{EV} = p_{\text{win}} - \frac{c + \texttt{fee\_cents}}{100}
    $$

  - Where $p_{\text{win}} = p_{\text{model}}$ for YES and $1-p_{\text{model}}$ for NO.
  - See `kalshi_edge/docs/metrics.md` and `kalshi_edge/docs/execution.md`.

---

## Quick start

From the **repo root** (the folder that contains `kalshi_edge/`):

```bash
python3 -m kalshi_edge.run
```

To see all CLI options:

```bash
python3 -m kalshi_edge.run --help
```

More run examples live in `kalshi_edge/docs/examples.md`.

---

## Documentation map

- **Start here**: `kalshi_edge/docs/README.md`
- **Methodology (pipeline + definitions)**: `kalshi_edge/docs/methodology.md`
- **Model**: `kalshi_edge/docs/model.md`
- **Metrics (EV, edge, etc.)**: `kalshi_edge/docs/metrics.md`
- **Execution assumptions (proxy pricing, depth diagnostics)**: `kalshi_edge/docs/execution.md`
- **Kalshi microstructure (as modeled here)**: `kalshi_edge/docs/microstructure.md`
- **Trading loop (optional)**: `kalshi_edge/docs/trading.md`
- **Limitations**: `kalshi_edge/docs/limitations.md`
- **Glossary**: `kalshi_edge/docs/glossary.md`

---

## Key files (where to look in code)

- `kalshi_edge/run.py`: CLI entrypoint and argument parsing
- `kalshi_edge/pipeline.py`: orchestration (`evaluate_event`)
- `kalshi_edge/kalshi_api.py`: event/market fetching + orderbooks
- `kalshi_edge/market_discovery.py`: optional “closing soon” discovery
- `kalshi_edge/market_state.py`: spot/vol sourcing + blending
- `kalshi_edge/math_models.py`: probability model helpers
- `kalshi_edge/ladder_eval.py`: ladder evaluation (orderbook stats + EV)
- `kalshi_edge/render.py`: CLI table formatting

---

## Trading (optional)

Trading automation is **off by default**. If enabled, the repo currently wires `--trade` to the V1 trader (`kalshi_edge/trader_v1.py`) and supports `--dry-run` logging.

See `kalshi_edge/docs/trading.md` for what’s implemented (entry/exit heuristics, state, logs).

---

## TODOs (intentionally not guessed)

- **TODO**: Replace `fee_cents` (flat per-contract) with Kalshi’s actual fee rule if you want production-like EVs (see `kalshi_edge/docs/metrics.md`).
- **TODO**: Document the exact CLI output table columns and how each is computed (docs currently note this gap; the source is `kalshi_edge/render.py`).