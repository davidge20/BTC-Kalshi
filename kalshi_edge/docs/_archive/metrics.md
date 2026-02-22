# Metrics (implementation-traced)

This doc only describes formulas that are directly traceable to code in this repo. Each metric section cites the implementing function/file.

## Units and conventions

- **Contract payoff**: a Kalshi binary settles to $\$1$ if the contract’s event occurs and $\$0$ otherwise.
- **Prices**:
  - Orderbook prices are in **cents** as integers (e.g. $63$ means $\$0.63$).
  - Convert cents → dollars by dividing by 100.
- **Fees**:
  - The implementation uses a constant **per-contract** fee `fee_cents` (integer cents) passed through the pipeline.
  - Convert `fee_cents` to dollars as $f = \tfrac{\texttt{fee\_cents}}{100}$.
  - **TODO**: This is not a price-dependent fee schedule in code today; if/when you implement Kalshi’s actual fee rule, update this doc and the functions that currently accept `fee_cents`.

## Model probability $p_{\text{model}}$

### Definition (in code)

In `kalshi_edge/math_models.py::lognormal_prob_above`, the model computes:

$$
p_{\text{model}} \;=\; \mathbb{P}(S_T \ge K)
$$

under a lognormal/zero-drift assumption:

$$
\ln\!\left(\frac{S_T}{S_0}\right)\sim \mathcal{N}(0,\sigma^2 t)
$$

The implemented closed-form is:

$$
p_{\text{model}}
= 1 - \Phi\!\left(\frac{\ln(K/S_0)}{\sigma\sqrt{t}}\right)
$$

where:

- $S_0$: current spot price in USD (from `kalshi_edge/market_state.py::deribit_index_price`).
- $K$: strike price in USD (parsed from the Kalshi ladder).
- $\sigma$: annualized volatility (decimal, e.g. $1.50$ means $150\%$), specifically `sigma_blend` from `kalshi_edge/market_state.py::build_market_state`.
- $t$: time-to-expiry in years, computed as:

$$
t = \frac{\texttt{minutes\_left}}{\texttt{MINUTES\_PER\_YEAR}}
$$

with `MINUTES_PER_YEAR` defined in `kalshi_edge/constants.py`.

### Side-specific win probability

When valuing a specific side:

- **YES wins** when ABOVE occurs: $p_{\text{win,YES}} = p_{\text{model}}$
- **NO wins** when ABOVE does not occur: $p_{\text{win,NO}} = 1 - p_{\text{model}}$

This convention is used in:
- `kalshi_edge/ladder_eval.py::evaluate_ladder` (YES vs NO EV)
- `kalshi_edge/trader_v1.py::_compute_p_win_now` (exit logic)

## Orderbook-derived “buy-now” proxy price (cents)

### Definition (in code)

In `kalshi_edge/ladder_eval.py::parse_orderbook_stats`, we compute top-of-book bids:

- $b_Y$: best **YES** bid in cents (`ybid`)
- $b_N$: best **NO** bid in cents (`nbid`)

Then we derive a **buy-now proxy** using the reciprocal bid:

$$
\texttt{ybuy} = 100 - b_N
\qquad
\texttt{nbuy} = 100 - b_Y
$$

These are the “executable buy price” inputs used for EV in `kalshi_edge/ladder_eval.py::ev_buy_binary`.

**TODO**: This is not the direct ask; it’s a reciprocal-bid proxy. If you later switch to using asks (or a different execution model), update both code and this doc.

## Buy-only EV per contract (dollars)

### Definition (in code)

In `kalshi_edge/ladder_eval.py::ev_buy_binary`:

$$
\mathrm{EV} = p_{\text{win}} - \frac{c + \texttt{fee\_cents}}{100}
$$

where:

- $p_{\text{win}}\in[0,1]$: win probability for the side (YES or NO).
- $c\in\{0,\dots,100\}$: buy price in cents (e.g. `ybuy` or `nbuy`).
- `fee_cents`: constant fee in cents per contract.
- $\mathrm{EV}$ is returned in **dollars per contract**.

Applied in `kalshi_edge/ladder_eval.py::evaluate_ladder` as:

$$
\mathrm{EV}_{\text{YES}} = p_{\text{model}} - \frac{\texttt{ybuy} + \texttt{fee\_cents}}{100}
\qquad
\mathrm{EV}_{\text{NO}} = (1-p_{\text{model}}) - \frac{\texttt{nbuy} + \texttt{fee\_cents}}{100}
$$

### Note on “probability points” vs dollars

Because the payoff is $\$1$, the numeric value of $\mathrm{EV}$ in dollars equals “probability points” (pp) on the $[0,1]$ scale (e.g. $0.05$ dollars ≡ 5pp).

This is relied on by `kalshi_edge/trader_v1.py`, which names this quantity `edge_pp` but sets it equal to $\mathrm{EV}$ (see next section).

## “edge_pp” (V1) is EV (not $p_{\text{model}} - c/100$)

### Definition (in code)

In `kalshi_edge/trader_v1.py::_pick_best_entry`, the candidate “edge” stored as `edge_pp` is:

$$
\texttt{edge\_pp} := \mathrm{EV}_{\text{YES}} \;\;\text{or}\;\; \mathrm{EV}_{\text{NO}}
$$

specifically:

- If YES is considered: `edge = float(row.ev_yes)`
- If NO is considered: `edge = float(row.ev_no)`

and the entry filter is `edge >= min_edge_pp`.

**TODO**: If you intend “edge_pp” to mean $p_{\text{win}} - c/100$ (pre-fee) instead of net EV, you’ll need to change the implementation and update this doc.

## EV% (V0 selection metric)

### Definition (in code)

In `kalshi_edge/trader_v0.py::_candidate_from_row`, for a candidate with EV in dollars $\mathrm{EV}$ and dollar cost:

$$
\text{cost} = \frac{c + \texttt{fee\_cents}}{100}
$$

the reported EV% is:

$$
\mathrm{EV}\% = \frac{\mathrm{EV}}{\text{cost}}
$$

and candidates are compared by $\mathrm{EV}\%$ in `kalshi_edge/trader_v0.py::pick_best_candidate`.

## Mark-to-market and PnL snapshot (V1)

### Mark-to-market net exit (in code)

In `kalshi_edge/trader_v1.py::snapshot_pnl`, if a position is open, the mark-to-market exit value uses the **best bid** for the held side, minus `fee_cents`:

$$
\text{net\_exit} = \frac{b - \texttt{fee\_cents}}{100}
$$

where $b$ is `ybid` for a YES position and `nbid` for a NO position (from `kalshi_edge/ladder_eval.py::parse_orderbook_stats`).

### Per-contract and total PnL (in code)

With `entry_cost` stored in state in **dollars per contract**:

$$
\mathrm{PnL}_{\text{per}} = \text{net\_exit} - \text{entry\_cost}
\qquad
\mathrm{PnL}_{\text{total}} = \mathrm{PnL}_{\text{per}} \cdot n
$$

where $n$ is the open contract count (from state).

## V1 exit thresholds (target/stop)

### Definition (in code)

In `kalshi_edge/trader_v1.py::_enter`, given:

- `entry_cost` $= \tfrac{\texttt{buy\_cents} + \texttt{fee\_cents}}{100}$
- `edge_pp` as defined above (net EV, dollars per contract)

the thresholds written to state are:

$$
\texttt{target\_pp} = \texttt{capture\_frac}\cdot \texttt{edge\_pp}
$$

$$
\texttt{stop\_pp} = \max(\texttt{min\_stop\_pp}, \texttt{stop\_frac}\cdot \texttt{edge\_pp})
$$

$$
\texttt{target\_net\_exit} = \text{entry\_cost} + \texttt{target\_pp}
\qquad
\texttt{stop\_net\_exit} = \text{entry\_cost} - \texttt{stop\_pp}
$$

## V1 “edge flip” quantity (exit heuristic)

### Definition (in code)

In `kalshi_edge/trader_v1.py::_try_exit`, the heuristic compares the model’s current win probability to the current net exit value:

$$
\texttt{hold\_advantage} = p_{\text{win,now}} - \texttt{net\_exit\_now}
$$

where:

- $p_{\text{win,now}}$ is from `kalshi_edge/trader_v1.py::_compute_p_win_now` (via `lognormal_prob_above`).
- $\texttt{net\_exit\_now} = \tfrac{\texttt{bid\_cents} - \texttt{fee\_cents}}{100}$.

Exit is triggered (if enabled) when:

$$
\texttt{hold\_advantage} \le -\texttt{edge\_flip\_pp}
$$
