"""
render.py

Pure rendering for terminal output.
No networking, no business logic.

If you later build a GUI, this file becomes your "view".
"""

from __future__ import annotations
import shutil
import sys
from typing import List, Optional

from kalshi_edge.ladder_eval import LadderRow
from kalshi_edge.pipeline import EvaluationResult


def fmt_cents(x: Optional[int]) -> str:
    return "-" if x is None else str(int(x))


def _clip_line(line: str, max_width: Optional[int]) -> str:
    if max_width is None or max_width <= 0:
        return line
    if len(line) <= max_width:
        return line
    if max_width <= 3:
        return line[:max_width]
    return line[: max_width - 3] + "..."


def render_once(
    result: EvaluationResult,
    sort_mode: str = "ev",
    *,
    compact: bool = False,
    show_explainer: bool = True,
    fit_to_terminal: bool = False,
    clear_screen: bool = False,
) -> None:
    ms = result.market_state

    max_width = None
    max_rows = None
    if fit_to_terminal and sys.stdout.isatty():
        term_size = shutil.get_terminal_size(fallback=(120, 40))
        max_width = term_size.columns
        summary_lines = 1 + (1 if (compact and ms.note) else 0) if compact else 12
        explainer_lines = 9 if (show_explainer and (not compact)) else 0
        table_lines = 3
        usable_lines = max(term_size.lines - 1, 5)
        max_rows = max(0, usable_lines - (summary_lines + table_lines + explainer_lines))

    if clear_screen and sys.stdout.isatty():
        print("\x1b[2J\x1b[H", end="")

    def emit(line: str = "") -> None:
        print(_clip_line(line, max_width))

    if compact:
        if ms.sigma_adjusted > 0:
            vol_label = "regression"
        elif ms.sigma_garch > 0:
            vol_label = "GARCH"
        else:
            vol_label = "blend"
        emit(
            f"{result.event_ticker} | left {ms.minutes_left:.1f}m | "
            f"spot ${ms.spot:,.2f} | {vol_label} {ms.sigma_blend*100:.1f}% | "
            f"1σ {ms.one_sigma_move_pct:.2f}% | {ms.confidence}"
        )
        if ms.note:
            emit(f"Notes: {ms.note}")
    else:
        emit("=== Summary ===")
        emit(f"Event ticker:          {result.event_ticker}")
        emit(f"Event title:           {result.event_title}")
        emit(f"UTC time:              {ms.ts_utc.isoformat()}")
        emit(f"Minutes left:          {ms.minutes_left:.2f}")
        emit(f"Deribit index (spot):  ${ms.spot:,.2f}")
        if ms.dvol_current > 0:
            emit(f"Deribit DVOL:          {ms.dvol_current*100:.1f}%")
        if ms.sigma_adjusted > 0:
            emit(f"Regression σ_adj:      {ms.sigma_adjusted*100:.1f}%  <-- primary")
        if ms.sigma_garch > 0:
            primary_tag = "  <-- primary" if ms.sigma_adjusted <= 0 else ""
            emit(f"GARCH(1,1) vol:        {ms.sigma_garch*100:.1f}%{primary_tag}")
        emit(f"Implied ATM vol:       {ms.sigma_implied*100:.1f}%")
        emit(f"Realized 1h vol:       {ms.sigma_realized*100:.1f}%")
        emit(f"Model vol (σ):         {ms.sigma_blend*100:.1f}%")
        emit(f"Confidence:            {ms.confidence}")
        emit(f"1σ move (time left):   ~{ms.one_sigma_move_pct:.2f}%")
        emit(f"Notes:                 {ms.note}")

    print_ladder_table(
        result.rows,
        spot=ms.spot,
        sort_mode=sort_mode,
        max_rows=max_rows,
        max_width=max_width,
    )
    if show_explainer and (not compact):
        print_explainer(max_width=max_width)


def print_ladder_table(
    rows: List[LadderRow],
    spot: float,
    sort_mode: str,
    *,
    max_rows: Optional[int] = None,
    max_width: Optional[int] = None,
) -> None:
    total = len(rows)
    if max_rows is not None and max_rows < total:
        rows = rows[:max_rows]
        title = f"Kalshi ABOVE ladder (sort={sort_mode}, showing {len(rows)}/{total})"
    else:
        title = f"Kalshi ABOVE ladder (sort={sort_mode})"
    print(_clip_line(title, max_width))
    has_mc = any(r.p_mc is not None for r in rows)
    hdr = (
        "Idx".rjust(4) +
        "  Ticker".ljust(34) +
        "Strike".rjust(10) +
        "   ΔK".rjust(8) +
        "     P".rjust(7) +
        ("  P_MC".rjust(7) if has_mc else "") +
        "  Sens".rjust(7) +
        "  Ybid".rjust(6) +
        "  Nbid".rjust(6) +
        "  Ybuy".rjust(6) +
        "  Nbuy".rjust(6) +
        "  SprY".rjust(6) +
        "  SprN".rjust(6) +
        "   EV_Y".rjust(8) +
        "   EV_N".rjust(8) +
        "  Rec".ljust(10) +
        "  Note"
    )
    print(_clip_line(hdr, max_width))
    if max_width is None:
        print("-" * len(hdr))
    else:
        print("-" * min(len(hdr), max_width))

    for i, r in enumerate(rows, 1):
        dk = r.strike - spot
        evy = "-" if r.ev_yes is None else f"{r.ev_yes:+.3f}"
        evn = "-" if r.ev_no is None else f"{r.ev_no:+.3f}"
        mc_col = f"{r.p_mc:7.3f}" if (has_mc and r.p_mc is not None) else ""
        line = (
            f"{i:4d}  " +
            r.ticker.ljust(34) +
            f"{r.strike:10,.0f}" +
            f"{dk:8,.0f}" +
            f"{r.p_model:7.3f}" +
            (mc_col if has_mc else "") +
            f"{r.sens:7.3f}" +
            f"{fmt_cents(r.ob.ybid):>6}" +
            f"{fmt_cents(r.ob.nbid):>6}" +
            f"{fmt_cents(r.ob.ybuy):>6}" +
            f"{fmt_cents(r.ob.nbuy):>6}" +
            f"{fmt_cents(r.ob.spread_y):>6}" +
            f"{fmt_cents(r.ob.spread_n):>6}" +
            f"{evy:>8}" +
            f"{evn:>8}" +
            f"  {r.rec.ljust(8)}" +
            f"  {r.rec_note}"
        )
        print(_clip_line(line, max_width))


def print_explainer(*, max_width: Optional[int] = None) -> None:
    print(_clip_line("", max_width))
    print(_clip_line("=== How to read the ladder columns ===", max_width))
    print(_clip_line(
        "P        : analytical probability that BTC >= strike at close (lognormal, σ from regression/GARCH).",
        max_width,
    ))
    print(_clip_line(
        "P_MC     : Monte Carlo probability (10k GBM paths, same σ). Cross-validation for P.",
        max_width,
    ))
    print(_clip_line(
        "Sens     : p*(1-p). Peaks at 0.25 when p≈0.50 (most informative strikes).",
        max_width,
    ))
    print(_clip_line("Ybid/Nbid: best bids (cents) on YES and NO from Kalshi.", max_width))
    print(_clip_line("Ybuy     : buy-now proxy for YES ≈ 100 - best NO bid.", max_width))
    print(_clip_line("Nbuy     : buy-now proxy for NO  ≈ 100 - best YES bid.", max_width))
    print(_clip_line("SprY/N   : implied spread = (buy proxy) - (bid). Smaller is better.", max_width))
    print(_clip_line("EV_Y/EV_N: expected value ($) after flat fee assumption, buy-only.", max_width))
