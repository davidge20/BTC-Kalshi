# Glossary (implementation-traced)

- **Event ticker**: Kalshi identifier for an event (e.g. `KXBTCD-...`). See `kalshi_edge/kalshi_api.py::get_event`.
- **Market ticker**: Kalshi identifier for a specific strike/market within an event. Used for orderbooks in `kalshi_edge/kalshi_api.py::get_orderbook`.
- **Ladder**: the set of threshold markets (strikes $K$) within an event returned by `kalshi_edge/kalshi_api.py::above_markets_from_event`.
- **Strike ($K$)**: strike in USD derived as $\text{round}(\texttt{floor\_strike}+0.01,2)$ in `kalshi_edge/kalshi_api.py::market_strike_from_floor`.
- **YES / NO**: the two sides of a binary contract. The repo models win probabilities as:
  - YES: $p_{\text{win}} = p_{\text{model}}$
  - NO: $p_{\text{win}} = 1-p_{\text{model}}$
  Used in `kalshi_edge/ladder_eval.py::evaluate_ladder` and `kalshi_edge/trader_v1.py::_compute_p_win_now`.
- **Executable price (cents)**: an integer price you can plausibly trade at now. For entries, the code uses a reciprocal-bid “buy-now proxy” rather than mid/ask (see next bullet).
- **Buy-now proxy (cents)**: derived entry price proxies from `kalshi_edge/ladder_eval.py::parse_orderbook_stats`:
  - `ybuy = 100 - nbid`
  - `nbuy = 100 - ybid`
- **EV (dollars/contract)**: buy-only expected value net of `fee_cents`, computed by `kalshi_edge/ladder_eval.py::ev_buy_binary`:
  $\mathrm{EV} = p_{\text{win}} - \frac{c+\texttt{fee\_cents}}{100}$.
- **“edge_pp” (V1)**: despite the name, V1 uses `edge_pp := EV` for entry selection (`kalshi_edge/trader_v1.py::_pick_best_entry`).
- **TODO**: If you want a separate “probability edge” definition like $p_{\text{win}}-c/100$, that is not currently computed/stored explicitly in code; implement it and then document it here.
