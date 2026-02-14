# Probability model

## Goal
Define the probability model used to estimate \(p_{\text{model}} = \mathbb{P}(S_T > K)\) over the remaining
time-to-expiry \(T\), and document the assumptions and parameters.

## Notation
- \(S_0\): current BTC spot price
- \(K\): ladder strike
- \(T\): time-to-expiry (in years)
- \(\sigma\): volatility over \([0,T]\)

## Model (lognormal / GBM-style baseline)
We use a simple lognormal baseline (GBM-style) to convert \((S_0, \sigma, T)\) into a probability of finishing above \(K\):
\[
p_{\text{model}} = \mathbb{P}(S_T > K)
\]

The intent is **not** full derivative pricing, but a fast, explainable baseline anchored to liquid BTC markets.

## Volatility (“blended vol”)
We often use a blended or stabilized \(\sigma\) rather than a single noisy quote. The exact blending method should match
implementation in `market_state.py` / `math_models.py`.

> TODO: Document the exact vol sourcing/blending once confirmed in code.

## Time-to-expiry \(T\)
Small errors in \(T\) can matter at short horizons. \(T\) should reflect the relevant Kalshi settlement time conventions.

> TODO: Document how expiry is parsed and converted into \(T\) once confirmed in code.

## Where this is implemented
- `market_state.py`: \(S_0, \sigma, T\) construction
- `math_models.py`: \(p_{\text{model}}\) computation

## Limitations
- Lognormal is a baseline; it ignores skew, jumps, and microstructure effects.
- Vol and \(T\) estimation dominate errors at short horizons.
