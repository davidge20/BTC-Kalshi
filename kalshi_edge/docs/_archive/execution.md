# Execution assumptions (implementation-traced)

This doc only describes formulas and conventions that are directly traceable to code in this repo.

## Units and conventions

- **Orderbook prices**: integer **cents** in $[0,100]$.
- **Dollars conversion**: dollars = cents / 100.
- **Fees**: the implementation uses a constant per-contract `fee_cents` (integer cents) passed into evaluation and trading.

## Entry pricing: buy-now proxy (cents)

Entry EV is computed using the buy-now proxy from `kalshi_edge/ladder_eval.py::parse_orderbook_stats`.

Define:
- $b_Y$: best YES bid in cents (`ybid`)
- $b_N$: best NO bid in cents (`nbid`)

Then:

$$
\texttt{ybuy} = 100 - b_N
\qquad
\texttt{nbuy} = 100 - b_Y
$$

If a required bid is missing, the corresponding proxy buy price is `None` and EV is not computed for that side.

## Spread diagnostics (cents)

`kalshi_edge/ladder_eval.py::parse_orderbook_stats` also derives “spread” diagnostics:

$$
\texttt{spread\_y} = \texttt{ybuy} - b_Y
\qquad
\texttt{spread\_n} = \texttt{nbuy} - b_N
$$

These are used for display/filtering (not for EV directly).

## Depth diagnostics (contracts within a cents window)

Depth is computed in `kalshi_edge/ladder_eval.py::parse_orderbook_stats` as the summed quantity within `depth_window_cents` of the best bid.

For a side with bid levels $(p_i, q_i)$ (price cents, quantity) and best bid $b$, define the cutoff:

$$
\text{cutoff} = b - \texttt{depth\_window\_cents}
$$

Then:

$$
\texttt{depth} = \sum_i q_i \cdot \mathbf{1}[p_i \ge \text{cutoff}]
$$

This produces:
- `depth_y` from YES levels relative to `ybid`
- `depth_n` from NO levels relative to `nbid`

**TODO**: The EV calculation itself is still top-of-book/proxy-based. There is no implemented slippage model that prices a multi-contract entry by walking the book.

## Exit pricing (when trading is enabled)

When a position is open, the trader uses best bid as the exit reference and subtracts `fee_cents` to form a net exit value in dollars.

In `kalshi_edge/trader_v1.py::snapshot_pnl` (and similarly in `_try_exit`), with:
- $b$ = best bid in cents on the held side (`ybid` for YES, `nbid` for NO)

the net exit is:

$$
\texttt{net\_exit} = \frac{b - \texttt{fee\_cents}}{100}
$$

## Where this is implemented

- `kalshi_edge/ladder_eval.py::parse_orderbook_stats`: proxy entry prices + spread/depth diagnostics
- `kalshi_edge/ladder_eval.py::ev_buy_binary`: EV uses proxy price + `fee_cents`
- `kalshi_edge/trader_v1.py`: best-bid-based exit checks and PnL snapshot
