# Trading loop (optional, implementation-traced)

This doc only describes trading logic that is directly traceable to code in this repo.

## Scope

Trading automation is optional and is only used when `kalshi_edge/run.py` is invoked with `--trade`.

As currently wired, `run.py` instantiates **V1** (`kalshi_edge/trader_v1.py::V1Trader`). V0 still exists as a simpler reference implementation (`kalshi_edge/trader_v0.py::V0Trader`).

## Shared units and conventions

- **Prices**:
  - cents: integer in $[0,100]$
  - dollars: cents / 100
- **Fee**: `fee_cents` is a constant per-contract fee in cents.
- **EV**: dollars per contract (see `docs/metrics.md`).
- **Order type**: orders are limit + fill-or-kill (`time_in_force="fill_or_kill"`) as constructed in:
  - `kalshi_edge/trader_v0.py::V0Trader.maybe_trade`
  - `kalshi_edge/trader_v1.py::V1Trader._place_order`

## V0 trader: buy-and-hold selection by EV%

Implemented in `kalshi_edge/trader_v0.py`.

### Candidate construction

From each `LadderRow`, `_candidate_from_row` constructs YES/NO candidates when proxy buy price and EV exist.

Cost in dollars per contract:

$$
\text{cost} = \frac{c + \texttt{fee\_cents}}{100}
$$

where $c$ is `row.ob.ybuy` (YES) or `row.ob.nbuy` (NO).

EV% is:

$$
\mathrm{EV}\% = \frac{\mathrm{EV}}{\text{cost}}
$$

where $\mathrm{EV}$ is `row.ev_yes` or `row.ev_no` (already net of fees in `ladder_eval.py::ev_buy_binary`).

### Filters and selection

`pick_best_candidate` applies (directly in code):
- `result.minutes_left >= min_minutes_left`
- `ev_pct >= min_ev_pct`
- `depth >= min_depth` (using `row.ob.depth_y` / `row.ob.depth_n`)
- optional `spread_cents <= max_spread_cents`

Then it chooses the single best candidate by highest `ev_pct`.

### State / reconciliation

V0 has `reconcile_state` which syncs a minimal state file against live positions (`kalshi_edge/kalshi_api.py::get_positions`).

**TODO**: V0’s state format differs from V1’s and doesn’t store `entry_cost`; the docs do not currently specify a stable state schema across versions.

## V1 trader: enter on net EV threshold, exit by capturing a fraction of EV

Implemented in `kalshi_edge/trader_v1.py`.

### Entry selection (“edge_pp”)

In `V1Trader._pick_best_entry`, the stored `edge_pp` is actually the net EV in dollars per contract:

$$
\texttt{edge\_pp} := \mathrm{EV}
$$

Specifically:
- YES candidate uses `edge_pp = row.ev_yes`
- NO candidate uses `edge_pp = row.ev_no`

It selects the single best candidate with:

$$
\texttt{edge\_pp} \ge \texttt{min\_edge\_pp}
$$

and `result.minutes_left >= min_minutes_left_entry`.

### Entry bookkeeping and thresholds

In `V1Trader._enter`, with:
- `buy_cents = cand.buy_cents`
- `fee_cents = self.fee_cents`

entry cost in dollars per contract is:

$$
\texttt{entry\_cost} = \frac{\texttt{buy\_cents} + \texttt{fee\_cents}}{100}
$$

The take-profit and stop thresholds (all in dollars per contract) are:

$$
\texttt{target\_pp} = \texttt{capture\_frac}\cdot \texttt{edge\_pp}
$$

$$
\texttt{stop\_pp} = \max(\texttt{min\_stop\_pp},\;\texttt{stop\_frac}\cdot \texttt{edge\_pp})
$$

And the corresponding net exit levels stored in state are:

$$
\texttt{target\_net\_exit} = \texttt{entry\_cost} + \texttt{target\_pp}
$$

$$
\texttt{stop\_net\_exit} = \texttt{entry\_cost} - \texttt{stop\_pp}
$$

### Exit triggers

In `V1Trader._try_exit`, exit can trigger for:
- take profit: `net_exit_now >= target_net_exit`
- stop loss: `net_exit_now <= stop_net_exit`
- time stop: `result.minutes_left <= exit_minutes_left`
- optional “edge flip” (see below)

The net exit reference uses best bid minus fees (dollars per contract):

$$
\texttt{net\_exit\_now} = \frac{\texttt{bid\_cents} - \texttt{fee\_cents}}{100}
$$

#### Edge flip heuristic

If enabled, `edge flip` uses:

$$
\texttt{hold\_advantage} = p_{\text{win,now}} - \texttt{net\_exit\_now}
$$

and exits when:

$$
\texttt{hold\_advantage} \le -\texttt{edge\_flip\_pp}
$$

where $p_{\text{win,now}}$ is computed by `V1Trader._compute_p_win_now` via `kalshi_edge/math_models.py::lognormal_prob_above`.

## Logging

V1 uses an append-only JSONL logger (`kalshi_edge/trade_log.py::TradeLogger`) and logs events such as:
- `bot_start`, `entry_signal`, `entry_filled`, `exit_filled`, `bot_shutdown`

See `kalshi_edge/trader_v1.py` for exact fields.
