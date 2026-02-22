# Kalshi market microstructure (implementation-traced)

This doc only describes formulas and conventions that are directly traceable to code in this repo.

## Units and conventions

- **Contract payoff**: each contract settles to $\$1$ if it wins and $\$0$ otherwise.
- **Price units**: Kalshi orderbook prices are integer **cents** in $[0,100]$.

## Contract semantics (as modeled here)

The system models an “ABOVE strike $K$” ladder and computes a model probability:

$$
p_{\text{model}} = \mathbb{P}(S_T \ge K)
$$

as implemented in `kalshi_edge/math_models.py::lognormal_prob_above`.

For side-specific win probability (used throughout the codebase):

- **YES**: $p_{\text{win,YES}} = p_{\text{model}}$
- **NO**: $p_{\text{win,NO}} = 1 - p_{\text{model}}$

Used in:
- `kalshi_edge/ladder_eval.py::evaluate_ladder`
- `kalshi_edge/trader_v1.py::_compute_p_win_now`

**TODO**: This is the repo’s modeling convention. The exact settlement wording (“above” vs “at or above”) is event-specific on Kalshi; if you want the docs to match the market rules text exactly, add a parser/validator for rule text and document it here.

## Thin books: why we don’t rely on asks

Kalshi ladders can be asymmetric: asks may be missing or stale while bids are present. The implementation therefore derives a conservative “buy-now” proxy from the opposite-side best bid instead of assuming a mid or best-ask.

## Buy-now proxy via reciprocal best bid (cents)

Implemented in `kalshi_edge/ladder_eval.py::parse_orderbook_stats`.

Define:

- $b_Y$: best YES bid in cents (`ybid`)
- $b_N$: best NO bid in cents (`nbid`)

The derived buy-now proxies are:

$$
\texttt{ybuy} = 100 - b_N
\qquad
\texttt{nbuy} = 100 - b_Y
$$

Interpretation:
- `ybuy` is the proxy “what you’d likely have to pay to buy YES now” (in cents).
- `nbuy` is the proxy “what you’d likely have to pay to buy NO now” (in cents).

This is an execution-aware approximation used as the entry price input to EV computations (see `docs/metrics.md` and `kalshi_edge/ladder_eval.py::ev_buy_binary`).

## Limitations / gotchas (as reflected by code)

- **Missing bids**: if $b_Y$ or $b_N$ is missing, the corresponding proxy price is `None` and EV cannot be computed for that side (`parse_orderbook_stats` + `ev_buy_binary`).
- **Depth**: the code computes a simple depth-within-window metric (`OrderbookStats.depth_y/depth_n`), but the EV calculation itself is still top-of-book/proxy-based (`kalshi_edge/ladder_eval.py::parse_orderbook_stats`).
