"""
Human-readable reporting for backtest results.
"""

from __future__ import annotations

from typing import List

from kalshi_edge.backtest_engine import BacktestSummary, EventResult


def _fmt_pct(x: float) -> str:
    return f"{100.0 * float(x):.2f}%"


def print_backtest_report(summary: BacktestSummary, top_n_events: int = 10) -> None:
    print("=== Backtest Summary ===")
    print(f"series:            {summary.series_ticker}")
    print(f"events scanned:    {summary.events_scanned}")
    print(f"events simulated:  {summary.events_simulated}")
    print(f"trades:            {summary.trades}")
    print(f"contracts:         {summary.contracts}")
    print(f"total pnl ($):     {summary.total_pnl:.4f}")
    pnl_per_trade = (summary.total_pnl / summary.trades) if summary.trades > 0 else 0.0
    print(f"pnl/trade ($):     {pnl_per_trade:.4f}")
    print(f"win rate:          {_fmt_pct(summary.win_rate)}")
    print(f"log jsonl:         {summary.log_path}")

    rows: List[EventResult] = sorted(summary.per_event, key=lambda r: (r.pnl, r.trades), reverse=True)
    if not rows:
        return

    print("")
    print("=== Top Events ===")
    for r in rows[: int(top_n_events)]:
        print(
            f"{r.event_ticker:<22} trades={r.trades:<3} contracts={r.contracts:<4} "
            f"pnl=${r.pnl:>8.4f} win_rate={_fmt_pct(r.win_rate)}"
        )
