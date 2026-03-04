## Paper trading (dry-run) + maker fill simulator

This project supports **paper trading** via `--dry-run`, which runs the full trading engine and logs decisions/fills, but **does not place real orders**.

### How to run

```bash
python3 -m kalshi_edge --config /path/to/config.json --watch --trade --dry-run
```

### What is simulated vs real

- **Real**:
  - market discovery, ladder evaluation (`p_model`, EV, spread, etc.)
  - order creation/cancel/refresh logic in the trading engine
  - risk caps and state persistence
  - JSONL logging
- **Simulated**:
  - fills for **resting maker orders** (optional; see below)
- **Not simulated**:
  - actual Kalshi matching, queue position, depth walk, partial fills driven by real flow, latency

### Maker fill simulation (`paper_fill_sim.py`)

When all of the following are true:

- you run with `--dry-run`
- `strategy.ORDER_MODE` is `maker_only` or `hybrid`
- `strategy.paper.simulate_maker_fills` is `true`

…the trader enables a lightweight fill simulator for **resting maker orders**.

The simulator is deliberately simple: it produces synthetic fills that flow through the same downstream bookkeeping path as real fills, so you can test the end-to-end system safely.

#### Inputs used

On each order refresh, the trader passes the simulator a top-of-book snapshot for the market side:

- **best bid (cents)** and a **best-ask proxy (cents)** derived from the current evaluation row
- a timestamp

The simulator is “book agnostic”; it trusts the caller to supply the relevant best bid/ask for the instrument side (YES vs NO).

#### Eligibility rule (top-of-book)

For a resting **buy** order with limit `price_cents`:

- **at top** if `price_cents >= best_bid_cents`
- **crossing** if `price_cents >= best_ask_cents`

If the order is neither at-top nor crossing, it is not eligible to fill.

#### Fill rule

There are two cases:

- **Crossing the ask** (`price_cents >= best_ask_cents`):
  - treated as an immediate synthetic execution (no waiting / no randomness)
- **At the bid** (`price_cents >= best_bid_cents` but not crossing):
  - the order must remain eligible for at least `paper.min_top_time_seconds`
  - after that, on each tick (rate-limited by `paper.tick_seconds`), it fills with probability `paper.fill_prob_per_tick`

This is intentionally a minimal heuristic:

- `min_top_time_seconds` approximates “you don’t fill instantly when you join the top”
- `fill_prob_per_tick` approximates “after you’re at the top, fills are stochastic”

#### Fill size and slippage

- **Partial fills**:
  - if `paper.partial_fill` is true, fill size is random in `[1, paper.max_fill_per_tick]` up to remaining size
  - otherwise the remaining size fills all at once
- **Slippage**:
  - `paper.slippage_cents` moves the fill price against you (adds for buys, subtracts for sells)

#### How to interpret the knobs

Once an order has been eligible for at least `min_top_time_seconds`, the simulator is effectively sampling a “fill arrival” each tick:

- tick duration $\Delta$ is `paper.tick_seconds`
- per-tick fill probability $p$ is `paper.fill_prob_per_tick`

An easy mental model for expected waiting time *after eligibility*:

$$
\mathbb{E}[\text{time to fill}] \approx \frac{\Delta}{p}
$$

Example: with `tick_seconds=1` and `fill_prob_per_tick=0.15`, the expected additional wait is ~6–7 seconds after the order has been at-top for `min_top_time_seconds`.

### When to trust paper trading (and when not to)

- **Good for**:
  - validating the trading engine logic end-to-end (refresh/cancel, caps, state, logging)
  - deterministic debugging with `paper.seed`
  - stress-testing behavior under different fill/partial-fill regimes
- **Not good for**:
  - forecasting realized PnL with maker execution accuracy
  - modeling queue dynamics or real depth/liquidity constraints

