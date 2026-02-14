# Kalshi market microstructure

## Goal
Explain the practical order-book and contract-semantic details that matter when converting Kalshi quotes
into “probability-like” prices and deciding whether an apparent edge is executable.

## Contract semantics (YES / NO)
For an “ABOVE \(K\)” market:
- YES settles to \(\$1\) if BTC settles **above \(K\)** at expiry, else \(\$0\).
- NO settles to \(\$1\) if BTC settles **at or below \(K\)** at expiry, else \(\$0\).

In a frictionless setting, YES and NO are complements:
\[
p_{\text{YES}} + p_{\text{NO}} = 1
\]
In practice, spreads + discrete ticks + fees mean the book is not perfectly complementary.

## Thin books: missing or stale asks
Kalshi ladders can be asymmetric:
- asks may be missing entirely
- asks may exist only at tiny size or stale levels
- one side can have “real” demand (bids) while the other looks empty

Relying on best-ask alone can therefore yield `None` prices or unrealistic EV calculations.

## Executable “buy-now proxy” via opposite-side bids
To stay execution-aware even when asks are missing/thin, we infer a conservative “buy-now” price
using the opposite side’s best bid and the YES/NO complement relationship:

- In cents, for an immediate **YES** buy:
\[
c_{\text{YES}} \approx 100 - \text{bestBid}(\text{NO})
\]
- In cents, for an immediate **NO** buy:
\[
c_{\text{NO}} \approx 100 - \text{bestBid}(\text{YES})
\]

This uses the most reliable live signal (active bids) to estimate a realistic entry cost.

**Important:** complements need not sum to 100 in practice. Treat this as an execution-aware approximation:
“could I likely get filled near here now?” rather than a frictionless theoretical price.

## Where this is implemented
- Look for “buy-now proxy”, “reciprocal bids”, or “implied ask” logic in `ladder_eval.py` and/or `pipeline.py`.
- Fee and rounding conventions often live in `constants.py`.

## Limitations / gotchas
- When the book is extremely thin, even best-bid can be noisy (1-lot levels).
- Depth matters: top-of-book may not represent the price for \(N\) contracts.
- Fees can break complement intuition even further (see [metrics](metrics.md)).
