#!/usr/bin/env python3
"""
TODO (REMOVE AFTER RESEARCH):
  This script currently PRETENDS that `event == "skip"` records were executed
  as fills ("paper trades"). This will over-count trades and is only for
  research / what-if analysis. Remove skip-as-fill logic when done.

analyze_pnl.py

Compute expected + realized PnL for trader_v2 "hold to expiration" entries.

Inputs:
  - trade_log.jsonl (TradeLogger JSONL)
    We treat:
      - event == "entry_filled" as real fills
      - event == "skip" as *hypothetical fills* (TEMP research behavior)

Optional:
  - outcomes file (jsonl) with lines like:
      {"market_ticker":"KXBTCD-26FEB1803-T67999.99", "result":"yes"}
    or {"market_ticker":"...", "outcome_yes": true}

Or:
  - --fetch-kalshi to fetch event/market results from Kalshi after resolution.

Notes:
  - Assumes $1 payout to the winning side at expiration, $0 otherwise.
  - entry_cost should include fees. For skip-record "fills", we compute:
        entry_cost = count * (price_cents + fee_cents)/100

New diagnostics / research flags (see --help):
  - --paper-skips / --no-paper-skips: toggle skip-as-fill research mode (default ON)
  - --exclude-skip-reason REASON (repeatable): ignore specific skip_reason from paper-skips
  - --dedupe-market: keep only first occurrence per market_ticker (ts_utc order)
  - --top-events N / --bottom-events N: top/bottom event_ticker by realized_pnl
  - --export-csv PATH: export per-position rows
  - --export-summary-json PATH: export aggregate stats + bin tables
  - --check-monotone / --no-check-monotone: ladder monotonicity sanity check (default ON if outcomes available)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Optional imports (only needed if --fetch-kalshi is used)
try:
    from kalshi_edge.http_client import HttpClient
    from kalshi_edge.kalshi_api import get_event
    from kalshi_edge.constants import KALSHI
except Exception:
    HttpClient = None
    get_event = None
    KALSHI = None


@dataclass
class Fill:
    ts_utc: str
    event_ticker: str
    market_ticker: str
    side: str          # "yes" or "no"
    count: int
    price_cents: int
    entry_cost: float  # dollars, includes fees
    fee: Optional[float]  # dollars (total fee for the trade if known)
    p_yes: float           # model P(YES) at entry
    ev: Optional[float]    # logged EV (dollars) if present
    implied_q: Optional[float]  # implied prob of chosen side from price (approx)
    source_event: str      # "entry_filled" or "skip"
    skip_reason: Optional[str]  # if source_event == "skip"

    def p_win_model(self) -> float:
        return self.p_yes if self.side == "yes" else (1.0 - self.p_yes)

    def expected_pnl(self) -> float:
        # expected payout is p_win * $1
        return float(self.count) * self.p_win_model() - float(self.entry_cost)


def read_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _compute_cost_from_cents(count: int, price_cents: int, fee_cents: int) -> float:
    return float(count) * float(price_cents + fee_cents) / 100.0


def parse_fills(
    log_path: str,
    *,
    include_dry_run: bool = True,
    include_skips_as_fills: bool = True,  # TEMP: research mode default ON
    exclude_skip_reasons: Optional[Sequence[str]] = None,
) -> List[Fill]:
    fills: List[Fill] = []
    exclude_set = {str(x) for x in (exclude_skip_reasons or [])}

    for rec in read_jsonl(log_path):
        evtype = rec.get("event")
        if evtype not in ("entry_filled", "skip"):
            continue
        if evtype == "skip":
            if not include_skips_as_fills:
                continue
            sr = rec.get("skip_reason")
            if sr is not None and str(sr) in exclude_set:
                continue

        if not include_dry_run and bool(rec.get("dry_run")):
            continue

        market_ticker = rec.get("market_ticker")
        if not isinstance(market_ticker, str):
            continue

        side = str(rec.get("side", "")).lower()
        if side not in ("yes", "no"):
            continue

        count = int(rec.get("count", 1))
        price_cents = int(rec.get("price_cents", 0))

        p_yes = rec.get("p")
        if not isinstance(p_yes, (int, float)):
            continue
        p_yes = float(p_yes)

        # entry_cost + fee handling differs by event type
        fee_dollars: Optional[float] = None
        entry_cost: Optional[float] = None

        if evtype == "entry_filled":
            ec = rec.get("entry_cost")
            if isinstance(ec, (int, float)):
                entry_cost = float(ec)

            f = rec.get("fee")
            if isinstance(f, (int, float)):
                fee_dollars = float(f)

            # fallback if entry_cost missing
            if entry_cost is None:
                # try infer from price_cents + fee (assume fee is total dollars)
                entry_cost = float(count) * (float(price_cents) / 100.0) + float(fee_dollars or 0.0)

        else:
            # evtype == "skip" -> pretend we filled at quoted top-of-book
            fee_cents = rec.get("fee_cents")
            if not isinstance(fee_cents, (int, float)):
                fee_cents = 0
            fee_cents = int(fee_cents)

            entry_cost = _compute_cost_from_cents(count, price_cents, fee_cents)
            fee_dollars = float(count) * float(fee_cents) / 100.0

        # implied probability of the chosen side from price (ignoring fee)
        implied_q = float(price_cents) / 100.0 if 0 <= price_cents <= 100 else None

        fills.append(
            Fill(
                ts_utc=str(rec.get("ts_utc", "")),
                event_ticker=str(rec.get("event_ticker", "")).upper(),
                market_ticker=market_ticker,
                side=side,
                count=count,
                price_cents=price_cents,
                entry_cost=float(entry_cost),
                fee=fee_dollars,
                p_yes=p_yes,
                ev=float(rec["EV"]) if isinstance(rec.get("EV"), (int, float)) else None,
                implied_q=implied_q,
                source_event=str(evtype),
                skip_reason=str(rec.get("skip_reason")) if evtype == "skip" and rec.get("skip_reason") is not None else None,
            )
        )

    return fills


def load_outcomes_jsonl(path: str) -> Dict[str, bool]:
    """
    Returns mapping: market_ticker -> outcome_yes (True if YES wins, False if NO wins)
    Accepts either:
      {"market_ticker":"...", "result":"yes"|"no"}
      {"market_ticker":"...", "outcome_yes": true|false}
    """
    out: Dict[str, bool] = {}
    for rec in read_jsonl(path):
        tkr = rec.get("market_ticker")
        if not isinstance(tkr, str):
            continue

        if isinstance(rec.get("outcome_yes"), bool):
            out[tkr] = bool(rec["outcome_yes"])
            continue

        r = rec.get("result")
        if isinstance(r, str):
            v = r.strip().lower()
            if v == "yes":
                out[tkr] = True
            elif v == "no":
                out[tkr] = False

    return out


def _parse_outcome_yes_from_market(m: Dict[str, Any]) -> Optional[bool]:
    for k in ("result", "resolution", "outcome", "settlement_result", "winning_outcome"):
        v = m.get(k)
        if isinstance(v, str):
            s = v.strip().lower()
            if s == "yes":
                return True
            if s == "no":
                return False

    for k in ("settlement_value", "settled_value", "final_value", "payout", "yes_payout"):
        v = m.get(k)
        if isinstance(v, (int, float)):
            if float(v) == 1.0:
                return True
            if float(v) == 0.0:
                return False

    return None


def fetch_outcomes_from_kalshi(event_tickers: List[str], *, debug_http: bool = False) -> Dict[str, bool]:
    if HttpClient is None or get_event is None:
        raise RuntimeError("kalshi_edge imports not available. Run inside your repo / venv where kalshi_edge is importable.")

    http = HttpClient(debug=debug_http)
    out: Dict[str, bool] = {}

    for et in sorted(set([e for e in event_tickers if e])):
        evj = get_event(http, et)
        markets = evj.get("markets") or (evj.get("event") or {}).get("markets") or []
        if not isinstance(markets, list):
            continue

        for m in markets:
            if not isinstance(m, dict):
                continue
            tkr = m.get("ticker") or m.get("market_ticker")
            if not isinstance(tkr, str):
                continue
            oy = _parse_outcome_yes_from_market(m)
            if oy is None:
                continue
            out[tkr] = oy

    return out


def _mean(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    return float(sum(xs)) / float(len(xs))


def _quantiles(xs: Sequence[float], ps: Sequence[float]) -> Dict[float, Optional[float]]:
    """
    Very small, deterministic "percentile-ish" quantiles.
    Uses nearest-rank index with floor, after sorting.
    """
    if not xs:
        return {float(p): None for p in ps}
    s = sorted(float(x) for x in xs)
    n = len(s)
    out: Dict[float, Optional[float]] = {}
    for p in ps:
        p = float(p)
        if p <= 0:
            out[p] = s[0]
            continue
        if p >= 1:
            out[p] = s[-1]
            continue
        idx = int(math.floor(p * (n - 1)))
        out[p] = s[idx]
    return out


def _parse_strike_from_market_ticker(market_ticker: str) -> Optional[float]:
    # Expected suffix: "...-T67999.99"  (float after "-T")
    if "-T" not in market_ticker:
        return None
    base, strike_s = market_ticker.rsplit("-T", 1)
    _ = base  # (unused) for clarity
    try:
        return float(strike_s)
    except Exception:
        return None


def _event_ticker_from_market_ticker(market_ticker: str) -> Optional[str]:
    # Heuristic for Kalshi ladder markets: event ticker is prefix before "-T<strike>"
    if "-T" not in market_ticker:
        return None
    base, _strike_s = market_ticker.rsplit("-T", 1)
    base = base.strip().upper()
    return base if base else None


def _compute_win(side: str, outcome_yes: bool) -> bool:
    return (side == "yes" and outcome_yes) or (side == "no" and (not outcome_yes))


def _compute_realized_pnl(count: int, entry_cost: float, win: bool) -> float:
    return (float(count) * (1.0 if win else 0.0)) - float(entry_cost)


def enrich_fills(fills: Sequence[Fill], outcomes: Dict[str, bool]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for f in fills:
        oy = outcomes.get(f.market_ticker)
        strike = _parse_strike_from_market_ticker(f.market_ticker)
        p_win_model = float(f.p_win_model())
        break_even_prob = (float(f.entry_cost) / float(f.count)) if f.count else None
        edge_prob = (p_win_model - float(break_even_prob)) if isinstance(break_even_prob, float) else None

        win: Optional[bool] = None
        realized_pnl: Optional[float] = None
        if oy is not None:
            win = _compute_win(f.side, bool(oy))
            realized_pnl = _compute_realized_pnl(f.count, f.entry_cost, win)

        rows.append(
            {
                "ts_utc": f.ts_utc,
                "event_ticker": f.event_ticker,
                "market_ticker": f.market_ticker,
                "strike": strike,
                "side": f.side,
                "count": int(f.count),
                "price_cents": int(f.price_cents),
                "entry_cost": float(f.entry_cost),
                "fee": float(f.fee) if isinstance(f.fee, (int, float)) else None,
                "p_yes": float(f.p_yes),
                "p_win_model": p_win_model,
                "implied_q_chosen_side": float(f.implied_q) if isinstance(f.implied_q, (int, float)) else None,
                "break_even_prob": break_even_prob,
                "edge_prob": edge_prob,
                "EV_logged": float(f.ev) if isinstance(f.ev, (int, float)) else None,
                "expected_pnl": float(f.expected_pnl()),
                "outcome_yes": bool(oy) if oy is not None else None,
                "win": win,
                "realized_pnl": realized_pnl,
                "source_event": f.source_event,
                "skip_reason": f.skip_reason,
            }
        )
    return rows


def aggregate(rows: Sequence[Dict[str, Any]], outcomes: Dict[str, bool]) -> Dict[str, Any]:
    market_tickers = [str(r["market_ticker"]) for r in rows]
    event_tickers = [str(r["event_ticker"]) for r in rows if str(r.get("event_ticker") or "")]
    src_counts = Counter(str(r.get("source_event") or "") for r in rows)

    total_cost_all = float(sum(float(r["entry_cost"]) for r in rows))
    total_expected_all = float(sum(float(r["expected_pnl"]) for r in rows))

    resolved_rows = [r for r in rows if r.get("win") is not None]
    resolved = int(len(resolved_rows))
    wins = int(sum(1 for r in resolved_rows if bool(r["win"])))
    losses = int(resolved - wins)
    realized_total = float(sum(float(r["realized_pnl"]) for r in resolved_rows)) if resolved_rows else 0.0
    total_cost_resolved = float(sum(float(r["entry_cost"]) for r in resolved_rows)) if resolved_rows else 0.0
    roi = (realized_total / total_cost_resolved) if total_cost_resolved > 0 else None

    avg_cost_all = _mean([float(r["entry_cost"]) for r in rows])
    avg_expected_all = _mean([float(r["expected_pnl"]) for r in rows])
    avg_realized_resolved = _mean([float(r["realized_pnl"]) for r in resolved_rows]) if resolved_rows else None
    avg_cost_resolved = _mean([float(r["entry_cost"]) for r in resolved_rows]) if resolved_rows else None
    avg_expected_resolved = _mean([float(r["expected_pnl"]) for r in resolved_rows]) if resolved_rows else None

    pwin_resolved = [float(r["p_win_model"]) for r in resolved_rows]
    win_rate = (float(wins) / float(resolved)) if resolved > 0 else None
    avg_pwin_resolved = _mean(pwin_resolved)
    calib_gap_pp = ((avg_pwin_resolved - win_rate) * 100.0) if (avg_pwin_resolved is not None and win_rate is not None) else None
    brier = None
    if resolved_rows:
        brier = _mean([(float(r["p_win_model"]) - (1.0 if bool(r["win"]) else 0.0)) ** 2 for r in resolved_rows])

    break_evens = [float(r["break_even_prob"]) for r in rows if isinstance(r.get("break_even_prob"), float)]
    edges = [float(r["edge_prob"]) for r in rows if isinstance(r.get("edge_prob"), float)]
    q_break_even = _quantiles(break_evens, [0.10, 0.25, 0.50, 0.75, 0.90])
    q_pwin = _quantiles([float(r["p_win_model"]) for r in rows], [0.10, 0.25, 0.50, 0.75, 0.90])

    return {
        "counts": {
            "positions": int(len(rows)),
            "unique_market_tickers": int(len(set(market_tickers))),
            "unique_event_tickers": int(len(set(event_tickers))),
            "by_source_event": {k: int(v) for k, v in sorted(src_counts.items())},
            "outcomes_available_markets": int(len(outcomes)),
            "resolved_positions": resolved,
        },
        "totals": {
            "total_cost_all": total_cost_all,
            "total_expected_pnl_all": total_expected_all,
            "total_cost_resolved": total_cost_resolved,
            "total_realized_pnl_resolved": realized_total,
        },
        "realized_stats_resolved": {
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_realized_pnl": avg_realized_resolved,
            "roi": roi,
        },
        "model_stats_resolved": {
            "avg_p_win_model": avg_pwin_resolved,
            "calibration_gap_pp": calib_gap_pp,
            "brier_score": brier,
        },
        "averages_all": {
            "avg_cost": avg_cost_all,
            "avg_expected_pnl": avg_expected_all,
            "avg_p_win_model": _mean([float(r["p_win_model"]) for r in rows]),
            "avg_break_even_prob": _mean(break_evens) if break_evens else None,
            "avg_edge_prob": _mean(edges) if edges else None,
        },
        "averages_resolved": {
            "avg_cost": avg_cost_resolved,
            "avg_expected_pnl": avg_expected_resolved,
        },
        "percentiles_all": {
            "p_win_model": {str(k): v for k, v in sorted(q_pwin.items())},
            "break_even_prob": {str(k): v for k, v in sorted(q_break_even.items())},
        },
    }


def group_stats(
    rows: Sequence[Dict[str, Any]],
    key_fn: Callable[[Dict[str, Any]], str],
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        k = str(key_fn(r))
        groups[k].append(r)

    out: List[Dict[str, Any]] = []
    for k in sorted(groups.keys()):
        g = groups[k]
        n = int(len(g))
        total_cost = float(sum(float(r["entry_cost"]) for r in g))
        exp_pnl = float(sum(float(r["expected_pnl"]) for r in g))
        resolved = [r for r in g if r.get("win") is not None]
        realized_pnl = float(sum(float(r["realized_pnl"]) for r in resolved)) if resolved else 0.0
        wins = int(sum(1 for r in resolved if bool(r["win"]))) if resolved else 0
        win_rate = (float(wins) / float(len(resolved))) if resolved else None
        avg_p = _mean([float(r["p_win_model"]) for r in g])
        out.append(
            {
                "key": k,
                "N": n,
                "total_cost": total_cost,
                "expected_pnl": exp_pnl,
                "realized_pnl": realized_pnl if resolved else None,
                "win_rate": win_rate,
                "avg_p_win_model": avg_p,
                "resolved": int(len(resolved)),
            }
        )
    return out


def _fmt_float(x: Optional[float], *, places: int = 4) -> str:
    if x is None:
        return ""
    return f"{float(x):.{places}f}"


def _fmt_pp(x: Optional[float], *, places: int = 2) -> str:
    if x is None:
        return ""
    return f"{float(x):+.{places}f}pp"


def _fmt_pct(x: Optional[float], *, places: int = 2) -> str:
    if x is None:
        return ""
    return f"{float(x)*100.0:.{places}f}%"


def _fmt_money(x: Optional[float], *, places: int = 4, signed: bool = False) -> str:
    if x is None:
        return ""
    s = f"{float(x):.{places}f}"
    if signed and not s.startswith("-"):
        s = "+" + s
    return "$" + s


def print_table(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[Tuple[str, str, Callable[[Any], str]]],
    *,
    title: Optional[str] = None,
    limit: Optional[int] = None,
) -> None:
    if title:
        print(title)
    if not rows:
        print("(empty)")
        return
    if limit is None:
        use_rows = list(rows)
    else:
        lim = int(limit)
        if lim <= 0:
            print("(empty)")
            return
        use_rows = list(rows[:lim])

    headers = [h for h, _k, _fmt in columns]
    table: List[List[str]] = []
    for r in use_rows:
        table.append([fmt(r.get(k)) for _h, k, fmt in columns])

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _is_numberlike(s: str) -> bool:
        return bool(re.match(r"^[\s\$\+\-]?\d", s))

    aligns_right = [_is_numberlike(h) for h in headers]
    for i in range(len(headers)):
        # Heuristic: right-align if column values look numeric
        aligns_right[i] = aligns_right[i] or any(_is_numberlike(r[i]) for r in table)

    head_cells = []
    for i, h in enumerate(headers):
        head_cells.append(h.rjust(widths[i]) if aligns_right[i] else h.ljust(widths[i]))
    print("  ".join(head_cells))
    print("  ".join(("-" * w) for w in widths))

    for row in table:
        out_cells = []
        for i, cell in enumerate(row):
            out_cells.append(cell.rjust(widths[i]) if aligns_right[i] else cell.ljust(widths[i]))
        print("  ".join(out_cells))


def calibration_bins_pwin(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resolved = [r for r in rows if r.get("win") is not None]
    bins: List[List[Dict[str, Any]]] = [[] for _ in range(10)]
    for r in resolved:
        p = float(r["p_win_model"])
        idx = int(min(9, max(0, math.floor(p * 10.0))))
        bins[idx].append(r)

    out: List[Dict[str, Any]] = []
    for i in range(10):
        lo = i / 10.0
        hi = (i + 1) / 10.0
        g = bins[i]
        n = len(g)
        if n == 0:
            out.append(
                {
                    "bin": f"[{lo:.1f},{hi:.1f})" if i < 9 else f"[{lo:.1f},{hi:.1f}]",
                    "N": 0,
                    "avg_p_win_model": None,
                    "realized_win_rate": None,
                    "diff_pp": None,
                }
            )
            continue
        avg_p = _mean([float(r["p_win_model"]) for r in g])
        wr = float(sum(1 for r in g if bool(r["win"]))) / float(n)
        out.append(
            {
                "bin": f"[{lo:.1f},{hi:.1f})" if i < 9 else f"[{lo:.1f},{hi:.1f}]",
                "N": int(n),
                "avg_p_win_model": avg_p,
                "realized_win_rate": wr,
                "diff_pp": ((avg_p - wr) * 100.0) if (avg_p is not None) else None,
            }
        )
    return out


def calibration_bins_edge(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resolved = [r for r in rows if r.get("win") is not None and isinstance(r.get("edge_prob"), float)]
    edges: List[Tuple[str, float, Optional[float], Optional[float]]] = [
        ("[-0.10,-0.05)", -0.10, -0.05, None),
        ("[-0.05,0)", -0.05, 0.0, None),
        ("[0,0.02)", 0.0, 0.02, None),
        ("[0.02,0.05)", 0.02, 0.05, None),
        ("[0.05,0.10)", 0.05, 0.10, None),
        ("[0.10,inf)", 0.10, None, None),
    ]
    bins: Dict[str, List[Dict[str, Any]]] = {name: [] for name, _lo, _hi, _ in edges}
    for r in resolved:
        e = float(r["edge_prob"])
        placed = False
        for name, lo, hi, _ in edges:
            if hi is None:
                if e >= lo:
                    bins[name].append(r)
                    placed = True
                    break
            else:
                if lo <= e < hi:
                    bins[name].append(r)
                    placed = True
                    break
        if not placed:
            # below lowest bin: ignore
            continue

    out: List[Dict[str, Any]] = []
    for name, _lo, _hi, _ in edges:
        g = bins[name]
        n = len(g)
        if n == 0:
            out.append({"bin": name, "N": 0, "avg_edge_prob": None, "win_rate": None, "realized_pnl_per_contract": None})
            continue
        wins = int(sum(1 for r in g if bool(r["win"])))
        wr = float(wins) / float(n)
        total_realized = float(sum(float(r["realized_pnl"]) for r in g))
        total_count = float(sum(float(r["count"]) for r in g))
        rpc = (total_realized / total_count) if total_count > 0 else None
        out.append(
            {
                "bin": name,
                "N": int(n),
                "avg_edge_prob": _mean([float(r["edge_prob"]) for r in g if isinstance(r.get("edge_prob"), float)]),
                "win_rate": wr,
                "realized_pnl_per_contract": rpc,
            }
        )
    return out


def break_even_bins(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    xs = [r for r in rows if isinstance(r.get("break_even_prob"), float)]
    # 10 bins in [0,1] plus overflow
    bins: List[List[Dict[str, Any]]] = [[] for _ in range(11)]
    for r in xs:
        be = float(r["break_even_prob"])
        if be < 0:
            idx = 0
        elif be >= 1.0:
            idx = 10
        else:
            idx = int(max(0, min(9, math.floor(be * 10.0))))
        bins[idx].append(r)

    out: List[Dict[str, Any]] = []
    for i in range(11):
        if i < 10:
            lo = i / 10.0
            hi = (i + 1) / 10.0
            name = f"[{lo:.1f},{hi:.1f})"
        else:
            name = "[1.0,inf)"
        g = bins[i]
        if not g:
            out.append({"bin": name, "N": 0, "avg_break_even_prob": None, "avg_p_win_model": None, "avg_edge_prob": None})
            continue
        out.append(
            {
                "bin": name,
                "N": int(len(g)),
                "avg_break_even_prob": _mean([float(r["break_even_prob"]) for r in g]),
                "avg_p_win_model": _mean([float(r["p_win_model"]) for r in g]),
                "avg_edge_prob": _mean([float(r["edge_prob"]) for r in g if isinstance(r.get("edge_prob"), float)]),
            }
        )
    return out


def check_monotonic_violations(outcomes: Dict[str, bool]) -> Dict[str, Any]:
    """
    For each event_ticker ladder (derived from market_ticker prefix before -T<strike>),
    ensure outcome_yes is monotone non-increasing as strike increases.
    """
    by_event: Dict[str, List[Tuple[float, bool, str]]] = defaultdict(list)
    for mt, oy in outcomes.items():
        if not isinstance(mt, str):
            continue
        strike = _parse_strike_from_market_ticker(mt)
        if strike is None:
            continue
        et = _event_ticker_from_market_ticker(mt)
        if not et:
            continue
        by_event[et].append((float(strike), bool(oy), mt))

    passing = 0
    failing = 0
    violations: List[Dict[str, Any]] = []
    for et in sorted(by_event.keys()):
        pts = sorted(by_event[et], key=lambda x: (x[0], x[2]))
        if len(pts) < 2:
            continue
        seq = [oy for _s, oy, _mt in pts]
        flips = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
        violation_idx: Optional[int] = None
        for i in range(1, len(seq)):
            if (seq[i - 1] is False) and (seq[i] is True):
                violation_idx = i
                break
        is_violation = (violation_idx is not None) or (flips > 1)
        if not is_violation:
            passing += 1
            continue
        failing += 1
        if violation_idx is None:
            # fall back to first change after two flips, if any
            seen = 0
            for i in range(1, len(seq)):
                if seq[i] != seq[i - 1]:
                    seen += 1
                    if seen >= 2:
                        violation_idx = i
                        break
        if violation_idx is None:
            violation_idx = 1
        lo = max(0, int(violation_idx) - 5)
        hi = min(len(pts), int(violation_idx) + 5)
        trace = [(pts[i][0], pts[i][1]) for i in range(lo, hi)]
        violations.append(
            {
                "event_ticker": et,
                "n_markets": int(len(pts)),
                "n_flips": int(flips),
                "trace": trace,
            }
        )
    return {
        "events_with_ladders": int(len(by_event)),
        "pass": int(passing),
        "fail": int(failing),
        "violations": violations,
    }


def export_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    fields = [
        "ts_utc",
        "event_ticker",
        "market_ticker",
        "strike",
        "side",
        "count",
        "price_cents",
        "entry_cost",
        "fee",
        "p_yes",
        "p_win_model",
        "implied_q_chosen_side",
        "break_even_prob",
        "edge_prob",
        "expected_pnl",
        "outcome_yes",
        "win",
        "realized_pnl",
        "source_event",
        "skip_reason",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {k: r.get(k) for k in fields}
            w.writerow(out)


def export_summary_json(path: str, summary: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="Path to trade_log.jsonl")
    ap.add_argument("--outcomes", default=None, help="Optional outcomes jsonl (market_ticker -> result)")
    ap.add_argument("--fetch-kalshi", action="store_true", help="Fetch outcomes from Kalshi by event ticker")
    ap.add_argument("--debug-http", action="store_true")
    ap.add_argument("--exclude-dry-run", action="store_true", help="Ignore dry_run fills")
    ap.add_argument("--print-positions", action="store_true")
    ap.add_argument("--paper-skips", dest="paper_skips", action="store_true", help="Treat skip events as hypothetical fills (research mode)")
    ap.add_argument("--no-paper-skips", dest="paper_skips", action="store_false", help="Do not paper-fill skip events")
    ap.set_defaults(paper_skips=True)
    ap.add_argument(
        "--exclude-skip-reason",
        action="append",
        default=[],
        help="Repeatable. Skip these skip_reason values from paper-skips (e.g. already_entered_market)",
    )
    ap.add_argument("--dedupe-market", action="store_true", help="Keep only first occurrence per market_ticker (ts_utc order)")
    ap.add_argument("--top-events", type=int, default=10, help="Print top-N event_ticker by realized_pnl (if resolved), else expected_pnl")
    ap.add_argument("--bottom-events", type=int, default=10, help="Print bottom-N event_ticker by realized_pnl (if resolved), else expected_pnl")
    ap.add_argument("--export-csv", default=None, help="Write per-position rows to CSV")
    ap.add_argument("--export-summary-json", default=None, help="Write aggregate summary + bins to JSON")
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument("--check-monotone", dest="check_monotone", action="store_true", help="Check ladder monotonicity (needs outcomes)")
    mg.add_argument("--no-check-monotone", dest="check_monotone", action="store_false", help="Disable ladder monotonicity check")
    ap.set_defaults(check_monotone=None)
    args = ap.parse_args()

    fills = parse_fills(
        args.log,
        include_dry_run=not args.exclude_dry_run,
        include_skips_as_fills=bool(args.paper_skips),  # TEMP research mode default ON
        exclude_skip_reasons=list(args.exclude_skip_reason or []),
    )
    if not fills:
        print("No entry_filled/skip records found.")
        return

    outcomes: Dict[str, bool] = {}
    if args.outcomes:
        outcomes.update(load_outcomes_jsonl(args.outcomes))

    if args.fetch_kalshi:
        event_tickers = [f.event_ticker for f in fills if f.event_ticker]
        outcomes.update(fetch_outcomes_from_kalshi(event_tickers, debug_http=args.debug_http))

    # Optional dedupe (by market_ticker, first ts_utc)
    if args.dedupe_market:
        fills_sorted = sorted(enumerate(fills), key=lambda t: (t[1].ts_utc, t[0]))
        seen: set[str] = set()
        deduped: List[Fill] = []
        for _idx, f in fills_sorted:
            if f.market_ticker in seen:
                continue
            seen.add(f.market_ticker)
            deduped.append(f)
        fills = deduped

    # Summary (expected)
    total_cost = sum(f.entry_cost for f in fills)
    total_exp = sum(f.expected_pnl() for f in fills)

    # Realized if outcomes known
    resolved = 0
    realized = 0.0
    wins = 0

    print(f"positions (including skip-as-fill): {len(fills)}  (dry_run_included={not args.exclude_dry_run})")
    print(f"total_cost: ${total_cost:.4f}")
    print(f"expected_pnl (model): ${total_exp:.4f}")

    if outcomes:
        for f in fills:
            oy = outcomes.get(f.market_ticker)
            if oy is None:
                continue
            resolved += 1
            win = (f.side == "yes" and oy) or (f.side == "no" and (not oy))
            wins += 1 if win else 0
            realized += (float(f.count) * (1.0 if win else 0.0)) - float(f.entry_cost)

        if resolved > 0:
            print(f"resolved: {resolved}/{len(fills)}  win_rate: {wins/resolved:.3%}")
            print(f"realized_pnl: ${realized:.4f}")
        else:
            print("resolved: 0 (outcomes provided, but none matched market_ticker strings)")
    else:
        print("resolved: 0 (provide --outcomes or use --fetch-kalshi after markets resolve)")

    # -----------------
    # Extended diagnostics (do not remove existing output above)
    # -----------------
    rows = enrich_fills(fills, outcomes)
    summary = aggregate(rows, outcomes)

    print("")
    print("=== diagnostics ===")
    print(f"unique market_tickers: {summary['counts']['unique_market_tickers']}")
    print(f"unique event_tickers: {summary['counts']['unique_event_tickers']}")
    print(f"counts by source_event: {json.dumps(summary['counts']['by_source_event'], ensure_ascii=False)}")

    if args.paper_skips:
        skip_reason_counts = Counter(str(f.skip_reason or "") for f in fills if f.source_event == "skip")
        # Also show "would be excluded" counts by scanning the log without applying exclusions.
        all_skip_reason_counts: Counter[str] = Counter()
        for rec in read_jsonl(args.log):
            if rec.get("event") != "skip":
                continue
            if not (not args.exclude_dry_run or not bool(rec.get("dry_run"))):
                # if exclude-dry-run is set, ignore dry_run records for this diagnostic too
                continue
            sr = rec.get("skip_reason")
            all_skip_reason_counts[str(sr) if sr is not None else ""] += 1

        if all_skip_reason_counts:
            print("")
            print("paper-skips research mode")
            print(f"exclude_skip_reason active: {sorted(set(str(x) for x in (args.exclude_skip_reason or [])))}")
            print_table(
                rows=[
                    {
                        "skip_reason": k if k else "(none)",
                        "N": int(v),
                        "share": (float(v) / float(sum(all_skip_reason_counts.values()))) if sum(all_skip_reason_counts.values()) > 0 else None,
                        "would_exclude_if_disabled": int(v),
                        "included_now": int(skip_reason_counts.get(k, 0)),
                    }
                    for k, v in sorted(all_skip_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                ],
                columns=[
                    ("skip_reason", "skip_reason", lambda x: str(x)),
                    ("N", "N", lambda x: str(int(x))),
                    ("share", "share", lambda x: _fmt_pct(x, places=2) if isinstance(x, float) else ""),
                    ("included_now", "included_now", lambda x: str(int(x))),
                    ("would_exclude", "would_exclude_if_disabled", lambda x: str(int(x))),
                ],
                title="skip_reason counts (and how many each --exclude-skip-reason would remove)",
            )

    if not args.dedupe_market:
        mt_counts = Counter(f.market_ticker for f in fills)
        dups = [(mt, c) for mt, c in mt_counts.items() if c > 1]
        if dups:
            dups_sorted = sorted(dups, key=lambda t: (-t[1], t[0]))[:10]
            print("")
            print(f"duplicate market_ticker diagnostics (dedupe OFF): {len(dups)} markets duplicated")
            for mt, c in dups_sorted:
                print(f"  {mt}: {c} occurrences")

    # High-level realized/model stats
    print("")
    print("high-level stats")
    rs = summary["realized_stats_resolved"]
    ms = summary["model_stats_resolved"]
    av = summary["averages_all"]
    avr = summary.get("averages_resolved", {})
    print(f"avg_cost (all): {_fmt_money(av.get('avg_cost'))}")
    print(f"avg_expected_pnl (all): {_fmt_money(av.get('avg_expected_pnl'), signed=True)}")
    if summary["counts"]["resolved_positions"] > 0:
        print(f"avg_cost (resolved): {_fmt_money(avr.get('avg_cost'))}")
        print(f"avg_expected_pnl (resolved): {_fmt_money(avr.get('avg_expected_pnl'), signed=True)}")
        print(f"wins/losses (resolved): {rs['wins']}/{rs['losses']}  win_rate: {_fmt_pct(rs.get('win_rate'))}")
        print(f"avg_realized_pnl (resolved): {_fmt_money(rs.get('avg_realized_pnl'), signed=True)}")
        print(f"ROI (resolved): {_fmt_float(rs.get('roi'), places=4)}")
        print(f"avg_p_win_model (resolved): {_fmt_float(ms.get('avg_p_win_model'), places=4)}")
        print(f"calibration gap (avg_p - win_rate): {_fmt_pp(ms.get('calibration_gap_pp'), places=2)}")
        print(f"brier score (resolved): {_fmt_float(ms.get('brier_score'), places=6)}")
    else:
        print("no resolved positions -> realized/model diagnostics skipped (provide outcomes)")

    # Break-even vs model probability
    print("")
    print("break-even probability vs model (all positions)")
    print(f"avg break_even_prob: {_fmt_float(av.get('avg_break_even_prob'), places=4)}")
    print(f"avg p_win_model: {_fmt_float(av.get('avg_p_win_model'), places=4)}")
    print(f"avg edge_prob (p_win_model - break_even): {_fmt_float(av.get('avg_edge_prob'), places=4)}")
    ptiles = summary.get("percentiles_all", {})
    if ptiles:
        print("percentiles (10/25/50/75/90)")
        be = ptiles.get("break_even_prob", {})
        pw = ptiles.get("p_win_model", {})
        print(f"  break_even_prob: {json.dumps(be, ensure_ascii=False)}")
        print(f"  p_win_model:     {json.dumps(pw, ensure_ascii=False)}")
        print("")
        print_table(
            rows=break_even_bins(rows),
            columns=[
                ("break-even bin", "bin", lambda x: str(x)),
                ("N", "N", lambda x: str(int(x))),
                ("avg_be", "avg_break_even_prob", lambda x: _fmt_float(x, places=4) if isinstance(x, float) else ""),
                ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x, places=4) if isinstance(x, float) else ""),
                ("avg_edge", "avg_edge_prob", lambda x: _fmt_float(x, places=4) if isinstance(x, float) else ""),
            ],
            title="break-even probability bins (all positions)",
        )

    # Breakdown tables
    print("")
    print("=== breakdown tables ===")
    by_side = group_stats(rows, lambda r: str(r.get("side") or ""))
    by_side = sorted(by_side, key=lambda r: (-int(r["N"]), str(r["key"])))
    print_table(
        rows=by_side,
        columns=[
            ("side", "key", lambda x: str(x)),
            ("N", "N", lambda x: str(int(x))),
            ("R", "resolved", lambda x: str(int(x))),
            ("total_cost", "total_cost", lambda x: _fmt_money(x)),
            ("expected_pnl", "expected_pnl", lambda x: _fmt_money(x, signed=True)),
            ("realized_pnl", "realized_pnl", lambda x: _fmt_money(x, signed=True) if isinstance(x, float) else ""),
            ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
            ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
        ],
        title="by side",
    )

    by_src = group_stats(rows, lambda r: str(r.get("source_event") or ""))
    by_src = sorted(by_src, key=lambda r: (-int(r["N"]), str(r["key"])))
    print("")
    print_table(
        rows=by_src,
        columns=[
            ("source_event", "key", lambda x: str(x)),
            ("N", "N", lambda x: str(int(x))),
            ("R", "resolved", lambda x: str(int(x))),
            ("total_cost", "total_cost", lambda x: _fmt_money(x)),
            ("expected_pnl", "expected_pnl", lambda x: _fmt_money(x, signed=True)),
            ("realized_pnl", "realized_pnl", lambda x: _fmt_money(x, signed=True) if isinstance(x, float) else ""),
            ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
            ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
        ],
        title="by source_event",
    )

    skip_rows = [r for r in rows if str(r.get("source_event") or "") == "skip"]
    by_skip_reason: List[Dict[str, Any]] = []
    if skip_rows:
        by_skip_reason = group_stats(skip_rows, lambda r: str(r.get("skip_reason") or ""))
        total_skips = float(len(skip_rows))
        for r in by_skip_reason:
            r["share"] = (float(r["N"]) / total_skips) if total_skips > 0 else None
        print("")
        print_table(
            rows=sorted(by_skip_reason, key=lambda r: (-int(r["N"]), str(r["key"]))),
            columns=[
                ("skip_reason", "key", lambda x: str(x) if str(x) else "(none)"),
                ("N", "N", lambda x: str(int(x))),
                ("R", "resolved", lambda x: str(int(x))),
                ("share", "share", lambda x: _fmt_pct(x, places=2) if isinstance(x, float) else ""),
                ("total_cost", "total_cost", lambda x: _fmt_money(x)),
                ("expected_pnl", "expected_pnl", lambda x: _fmt_money(x, signed=True)),
                ("realized_pnl", "realized_pnl", lambda x: _fmt_money(x, signed=True) if isinstance(x, float) else ""),
                ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
                ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
            ],
            title="by skip_reason (source_event==skip)",
        )

    # By event_ticker (top/bottom by realized if possible)
    by_event = group_stats(rows, lambda r: str(r.get("event_ticker") or ""))
    can_sort_by_realized = any(isinstance(r.get("realized_pnl"), float) for r in by_event)
    metric = "realized_pnl" if can_sort_by_realized else "expected_pnl"
    by_event_sorted = sorted(
        by_event,
        key=lambda r: (
            -(float(r[metric]) if isinstance(r.get(metric), float) else float("-inf")),
            str(r["key"]),
        ),
    )
    print("")
    print_table(
        rows=by_event_sorted,
        columns=[
            ("event_ticker", "key", lambda x: str(x)),
            ("N", "N", lambda x: str(int(x))),
            ("R", "resolved", lambda x: str(int(x))),
            ("total_cost", "total_cost", lambda x: _fmt_money(x)),
            ("expected_pnl", "expected_pnl", lambda x: _fmt_money(x, signed=True)),
            ("realized_pnl", "realized_pnl", lambda x: _fmt_money(x, signed=True) if isinstance(x, float) else ""),
            ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
            ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
        ],
        title=f"by event_ticker (TOP {args.top_events} by {metric})",
        limit=int(args.top_events or 0),
    )

    if int(args.bottom_events or 0) > 0:
        by_event_bottom = sorted(
            by_event,
            key=lambda r: (
                (float(r[metric]) if isinstance(r.get(metric), float) else float("inf")),
                str(r["key"]),
            ),
        )
        print("")
        print_table(
            rows=by_event_bottom,
            columns=[
                ("event_ticker", "key", lambda x: str(x)),
                ("N", "N", lambda x: str(int(x))),
                ("R", "resolved", lambda x: str(int(x))),
                ("total_cost", "total_cost", lambda x: _fmt_money(x)),
                ("expected_pnl", "expected_pnl", lambda x: _fmt_money(x, signed=True)),
                ("realized_pnl", "realized_pnl", lambda x: _fmt_money(x, signed=True) if isinstance(x, float) else ""),
                ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
                ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
            ],
            title=f"by event_ticker (BOTTOM {args.bottom_events} by {metric})",
            limit=int(args.bottom_events or 0),
        )

    # Calibration / binning report
    if summary["counts"]["resolved_positions"] > 0:
        print("")
        print("=== calibration / binning ===")
        pwin_bins = calibration_bins_pwin(rows)
        print_table(
            rows=pwin_bins,
            columns=[
                ("p_win bin", "bin", lambda x: str(x)),
                ("N", "N", lambda x: str(int(x))),
                ("avg_p", "avg_p_win_model", lambda x: _fmt_float(x) if isinstance(x, float) else ""),
                ("win_rate", "realized_win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
                ("diff", "diff_pp", lambda x: _fmt_pp(x) if isinstance(x, float) else ""),
            ],
            title="by p_win_model decile (resolved only)",
        )

        edge_bins = calibration_bins_edge(rows)
        print("")
        print_table(
            rows=edge_bins,
            columns=[
                ("edge bin", "bin", lambda x: str(x)),
                ("N", "N", lambda x: str(int(x))),
                ("avg_edge", "avg_edge_prob", lambda x: _fmt_float(x, places=4) if isinstance(x, float) else ""),
                ("win_rate", "win_rate", lambda x: _fmt_pct(x) if isinstance(x, float) else ""),
                ("real_pnl/ct", "realized_pnl_per_contract", lambda x: _fmt_money(x, places=6, signed=True) if isinstance(x, float) else ""),
            ],
            title="by edge_prob = p_win_model - break_even_prob (resolved only)",
        )

    # Ladder monotonicity sanity check
    check_monotone = bool(outcomes) if args.check_monotone is None else bool(args.check_monotone)
    if check_monotone and outcomes:
        mono = check_monotonic_violations(outcomes)
        print("")
        print("=== monotonic violations (ladder sanity check) ===")
        print(f"events_with_ladders: {mono['events_with_ladders']}  pass: {mono['pass']}  fail: {mono['fail']}")
        viols = mono["violations"][:20]
        if not viols:
            print("no violations detected")
        else:
            for v in viols:
                trace = ", ".join([f"({s:g},{'Y' if oy else 'N'})" for s, oy in v["trace"]][:10])
                print(f"{v['event_ticker']}  markets={v['n_markets']}  flips={v['n_flips']}  trace={trace}")
        # include in summary JSON export if requested
        summary["monotone"] = mono

    # Exports
    if args.export_csv:
        export_csv(str(args.export_csv), rows)
        print("")
        print(f"wrote CSV: {args.export_csv}")
    if args.export_summary_json:
        # attach grouped tables + bins for offline analysis
        summary_export = dict(summary)
        summary_export["tables"] = {
            "by_side": by_side,
            "by_source_event": by_src,
            "by_skip_reason": by_skip_reason if skip_rows else [],
            "by_event_ticker": by_event,
        }
        summary_export["bins"] = {"break_even_prob": break_even_bins(rows)}
        if summary["counts"]["resolved_positions"] > 0:
            summary_export["bins"].update({"p_win_model": calibration_bins_pwin(rows), "edge_prob": calibration_bins_edge(rows)})
        export_summary_json(str(args.export_summary_json), summary_export)
        print("")
        print(f"wrote summary JSON: {args.export_summary_json}")

    if args.print_positions:
        for f in fills:
            oy = outcomes.get(f.market_ticker)
            win = None
            realized_pnl = None
            if oy is not None:
                win = (f.side == "yes" and oy) or (f.side == "no" and (not oy))
                realized_pnl = (float(f.count) * (1.0 if win else 0.0)) - float(f.entry_cost)

            row = {
                "ts_utc": f.ts_utc,
                "event_ticker": f.event_ticker,
                "market_ticker": f.market_ticker,
                "strike": _parse_strike_from_market_ticker(f.market_ticker),
                "side": f.side,
                "count": f.count,
                "price_cents": f.price_cents,
                "entry_cost": round(f.entry_cost, 6),
                "fee": round(f.fee, 6) if isinstance(f.fee, float) else None,
                "p_yes": round(f.p_yes, 6),
                "p_win_model": round(f.p_win_model(), 6),
                "implied_q_chosen_side": round(f.implied_q, 6) if isinstance(f.implied_q, float) else None,
                "break_even_prob": round((f.entry_cost / f.count), 6) if f.count else None,
                "edge_prob": round((f.p_win_model() - (f.entry_cost / f.count)), 6) if f.count else None,
                "EV_logged": round(f.ev, 6) if isinstance(f.ev, float) else None,
                "expected_pnl": round(f.expected_pnl(), 6),
                "outcome_yes": oy,
                "win": win,
                "realized_pnl": round(realized_pnl, 6) if isinstance(realized_pnl, float) else None,
                "source_event": f.source_event,
                "skip_reason": f.skip_reason,
            }
            print(json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
