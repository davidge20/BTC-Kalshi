# Limitations (implementation-informed)

This document is intentionally qualitative, but when it mentions a specific quantity, it reflects the current implementation.

## Market structure / data quality

- **Thin books**: if best bids are missing, the code cannot derive proxy buy prices (`kalshi_edge/ladder_eval.py::parse_orderbook_stats`) and EV becomes `None` for that side.
- **Top-of-book noise**: the EV computation is based on proxy entry prices derived from best bids, not on depth-walked execution (`kalshi_edge/ladder_eval.py::ev_buy_binary`).
- **Depth vs size**: depth is computed as a diagnostic (`OrderbookStats.depth_y/depth_n`), but it is not used to adjust EV for larger $N$.

## Modeling assumptions

- **Lognormal baseline**: probabilities come from `kalshi_edge/math_models.py::lognormal_prob_above` and do not model skew/jumps.
- **Volatility inputs**: the outcome depends heavily on the Deribit/Coinbase inputs and the blend rule in `kalshi_edge/market_state.py`.
- **Time alignment**: `minutes_left` is derived from Kalshi `close_time` fields (`kalshi_edge/kalshi_api.py::above_markets_from_event`), which may or may not perfectly match settlement conventions.

## Fees and execution

- **Fee model is simplified**: the repo currently uses a constant per-contract `fee_cents` everywhere (see `docs/metrics.md`). Real Kalshi fee rules may be price/side dependent.
- **Exits are not guaranteed**: V1 exits require a bid to exist on the held side (`kalshi_edge/trader_v1.py::_try_exit`); thin books can block exits even if model EV changes.

## Operational considerations

- **API failures**: network/API errors can disrupt orderbook fetches and trading actions; warnings are logged/printed in `ladder_eval.py` and `trader_v1.py`.
- **Rule text**: this repo does not parse/validate event rule text. **TODO**: if you need rule-faithful semantics (e.g. strict $>$ vs $\ge$), implement validation and document it.
