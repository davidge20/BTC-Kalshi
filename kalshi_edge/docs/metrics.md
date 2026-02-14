# Metrics: edge and EV

## Goal
Define the project’s conventions for “edge” and “expected value” (EV), including units, fee treatment,
and conservative execution assumptions.

## Notation
Let \(p_{\text{model}}\) denote the model probability that BTC settles **ABOVE** the strike at expiry:
\[
p_{\text{model}} = \mathbb{P}(\text{ABOVE})
\]

Let \(c \in [0,100]\) be an **executable buy price** in cents for a 1-contract position on a given side (YES or NO),
using our buy-now proxy (and/or direct ask if available). Define the market-implied probability from that executable price as:
\[
p_{\text{mkt}}(c) \approx \frac{c}{100}
\]

## Edge in probability points
We define “edge” as:
\[
\text{edge}_{pp} = p_{\text{model}} - p_{\text{mkt}}(c)
\]
Reported in probability points (e.g., \(+0.04\) means +4pp).

## Expected value (EV) per contract, net of fees
Kalshi binaries settle to \(\$1\) if the event occurs and \(\$0\) otherwise.

For a **YES** buy at cost \(c/100\) dollars:
\[
EV_{\text{YES}} = p_{\text{model}}\cdot 1 - \frac{c}{100} - \text{fees}(c)
\]

For a **NO** buy (pays \(\$1\) if ABOVE does *not* occur):
\[
EV_{\text{NO}} = (1 - p_{\text{model}})\cdot 1 - \frac{c}{100} - \text{fees}(c)
\]

> TODO: Replace \(\text{fees}(c)\) with the exact implemented fee function once verified in code.

## Execution assumptions (intentionally conservative)
We compute EV under **buy-only, taker-style** assumptions:
- Entry pricing uses executable “buy-now” proxy prices (not mid).
- Kalshi fees are subtracted explicitly.
- We intentionally do **not** assume maker fills or frictionless exits, since those assumptions can overstate achievable edge in thin books.

## Where this is implemented
- `ladder_eval.py`: edge/EV computation
- `constants.py`: fee parameters (and possibly helper functions)
- `trader_v0.py` / `trader_v1.py`: realized trade logging / net entry-exit accounting
