# Methodology

## Goal
Identify potential mispricings in Kalshi BTC ladder markets by comparing **executable** market prices
to a model-implied probability derived from deeper BTC venues (spot + options-implied volatility),
net of fees and realistic execution assumptions.

## Pipeline (high level)
1. **Discover** a relevant Kalshi event (often one that is closing soon).
2. Build a **MarketState**: spot \(S_0\), volatility \(\sigma\), and time-to-expiry \(T\).
3. For each ladder strike \(K\):
   - infer an executable buy price \(c\) (cents) using a buy-now proxy when needed
   - compute a model probability \(p_{\text{model}} = \mathbb{P}(S_T > K)\)
   - compute edge and fee-adjusted expected value (EV)
4. **Render** ranked opportunities (and optionally feed into a trading loop).

## Key decisions (design intent)
- Use an execution-aware **buy-now proxy** when asks are missing/thin (see [microstructure](microstructure.md)).
- Compute EV under **buy-only, taker-style** assumptions to stay conservative (see [metrics](metrics.md)).
- Treat output as a **research signal**, not guaranteed arbitrage; liquidity and fees dominate in practice.

## Implementation map
- `market_discovery.py`: event discovery
- `market_state.py`: \(S_0\), \(\sigma\), \(T\) construction
- `math_models.py`: \(p_{\text{model}}\) computation
- `ladder_eval.py`: strike loop + EV/edge
- `pipeline.py`: orchestration
- `render.py`: output tables
- `trader_v0.py` / `trader_v1.py`: optional automation

## Limitations / gotchas (top-level)
- Thin books can make executable pricing noisy; always interpret results with liquidity in mind.
- Fee modeling matters: “positive edge” can become negative EV after fees.
- Settlement conventions (timing, reference index) can differ from intuition; always validate event rules.
