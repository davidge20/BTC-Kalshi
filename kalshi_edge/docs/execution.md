# Execution assumptions

## Goal
Document how we translate observed order books into executable prices and why we choose conservative conventions.

## Buy-now pricing
We estimate an executable buy price \(c\) in cents for each side (YES/NO). When asks are missing or unreliable,
we infer \(c\) using opposite-side best bids (see [microstructure](microstructure.md)).

## Taker-style convention
Unless explicitly stated, we assume taker-style execution:
- We pay a price consistent with crossing the spread / taking available liquidity.
- We avoid assuming fills at mid, since that can create “paper” edge in thin books.

## Depth and slippage
Top-of-book can differ from the price for \(N\) contracts.

> TODO: If the implementation supports depth-aware pricing (e.g., price for N contracts), document it here.

## Exits (why they’re not modeled optimistically)
This project focuses on identifying mispricings at entry. We avoid assuming perfect exits or immediate convergence,
since liquidity constraints can dominate realized outcomes.

## Where this is implemented
- `ladder_eval.py`: buy-now proxy / executable price inference
- `trader_v1.py`: exit logic (if enabled)
