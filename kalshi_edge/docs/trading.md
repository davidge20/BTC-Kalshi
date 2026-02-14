# Trading loop (optional)

## Goal
Describe the optional automated trading loop(s) (`trader_v0.py`, `trader_v1.py`) and their conservative risk/execution logic.

## Scope
This project is primarily a **research signal generator**. Trading automation is optional and should be treated as experimental.

## Typical loop structure
1. Snapshot market state and ladder.
2. Compute edge/EV by strike.
3. Apply thresholds (minimum EV, liquidity checks, max positions).
4. Place orders (often buy-only entries).
5. Reconcile fills and update position state.
6. Consider exits according to strategy logic and book conditions.

> TODO: Fill in exact entry/exit rules once verified in `trader_v1.py`.

## Logging
Trade/log events should be durable and structured (JSONL is common), including entry cost, fees, and realized PnL.

## Limitations
- Thin books can prevent exits even when model edge mean-reverts.
- Risk controls (position limits, time stops) matter.
