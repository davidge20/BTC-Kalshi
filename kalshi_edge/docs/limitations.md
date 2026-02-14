# Limitations

## Goal
List the main sources of error and why “edge” does not imply guaranteed profit.

## Market structure
- Thin books, stale quotes, and one-lot top-of-book noise.
- Spread and discrete ticks break complement intuition.

## Modeling assumptions
- Lognormal baseline ignores skew/jumps.
- Vol and time-to-expiry estimation errors matter at short horizons.

## Fees and execution
- Fees can flip sign of EV for small edges.
- Exits are not guaranteed; liquidity is the dominant constraint.

## Operational considerations
- APIs can fail; retry/backoff and logging are critical.
- Event settlement conventions must be validated per market.
