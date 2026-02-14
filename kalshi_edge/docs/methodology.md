# Methodology (implementation-traced)

This doc only describes logic and formulas that are directly traceable to code in this repo.

## Goal

Evaluate a Kalshi BTC “ABOVE strike” ladder by comparing an **execution-aware entry price** (in cents) to a simple model probability derived from deeper BTC venues (spot + implied/realized volatility), producing **buy-only EV** metrics net of a constant per-contract fee.

## Primary entrypoint

The high-level orchestration is `kalshi_edge/pipeline.py::evaluate_event`.

## Event discovery (optional)

If you don’t pass `--event` or `--url`, `kalshi_edge/run.py::main` calls:

- `kalshi_edge/market_discovery.py::discover_current_event(window_minutes=...)`

Discovery scans Kalshi markets closing soon and chooses the soonest-closing BTC above/below family it finds (see code for exact filters).

## Time to close: `minutes_left` (minutes)

When evaluating an event, `kalshi_edge/kalshi_api.py::above_markets_from_event` computes:

$$
\texttt{minutes\_left} = \max\!\left(0,\;\frac{\texttt{close\_time} - \texttt{now}}{60}\right)
$$

where:
- `close_time` is the minimum `close_time` across the ladder’s ABOVE threshold markets.
- `now` is the current UTC time.

Units:
- `minutes_left` is in **minutes** (float).

## Strike conversion: `floor_strike` → `strike` (USD)

Kalshi’s KXBTCD threshold markets expose `floor_strike`. In `kalshi_edge/kalshi_api.py::market_strike_from_floor` the code converts:

$$
K = \text{round}(\texttt{floor\_strike} + 0.01,\;2)
$$

Units:
- `floor_strike` and $K$ are in **USD**.

## Market state: spot and volatility

`kalshi_edge/market_state.py::build_market_state` constructs:

- spot price $S_0$ (USD) from Deribit index (`deribit_index_price`)
- implied vol $\sigma_{\text{implied}}$ (annualized decimal) from Deribit options (`deribit_atm_implied_vol`)
- realized vol $\sigma_{\text{realized}}$ (annualized decimal) from Coinbase 1-minute candles (`coinbase_realized_vol_1h`)
- blended vol $\sigma_{\text{blend}}$ and a confidence label (`blend_vol`, `confidence_label`)

See `docs/model.md` for the formulas that are explicitly implemented.

## Ladder evaluation: price proxies, probabilities, EV

`kalshi_edge/ladder_eval.py::evaluate_ladder` does, for each chosen strike:

- compute $p_{\text{model}}$ via `kalshi_edge/math_models.py::lognormal_prob_above` (clamped to $[0,1]$)
- parse orderbook and derive proxy entry prices (`parse_orderbook_stats`)
- compute buy-only EV in dollars per contract (`ev_buy_binary`)

The EV formulas and units are documented in `docs/metrics.md`.

## Which strikes are evaluated

The ladder can be large, so strikes are selected in `kalshi_edge/ladder_eval.py::pick_markets_near_spot` by preferring those within a band around spot:

$$
\text{lo} = S_0\left(1 - \frac{\texttt{band\_pct}}{100}\right)
\qquad
\text{hi} = S_0\left(1 + \frac{\texttt{band\_pct}}{100}\right)
$$

## Output rendering

`kalshi_edge/render.py::render_once` turns `EvaluationResult` into the CLI table output.

**TODO**: The docs currently don’t specify the exact table columns and how each is computed. If you want those documented “implementation-traced,” add a section that maps each column to the exact `LadderRow` fields / formatting helpers in `render.py`.
