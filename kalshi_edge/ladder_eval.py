"""
ladder_eval.py

Evaluate a Kalshi ABOVE ladder given:
- the ladder markets (with strike info)
- a MarketState (spot + sigma_blend)
- minutes_left

This module is responsible for:
- fetching orderbooks (concurrently)
- deriving buy-now proxy prices from reciprocal bids
- computing EV for buy-only trades
- returning rows ready for rendering
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from kalshi_edge.http_client import HttpClient
from kalshi_edge.data.kalshi.client import get_orderbook
from kalshi_edge.data.kalshi.models import market_strike_from_floor
from kalshi_edge.math_models import clamp01, lognormal_prob_above


@dataclass
class OrderbookStats:
    """
    Derived top-of-book stats.

    Kalshi returns bids on YES and NO.
    We derive a buy-now proxy by using the reciprocal best bid:

      implied YES buy price  ~= 100 - best NO bid
      implied NO  buy price  ~= 100 - best YES bid
    """
    ybid: Optional[int]
    yqty: Optional[float]
    nbid: Optional[int]
    nqty: Optional[float]
    ybuy: Optional[int]
    nbuy: Optional[int]
    spread_y: Optional[int]
    spread_n: Optional[int]
    depth_y: float
    depth_n: float
    levels_y: int
    levels_n: int
    note: str


@dataclass
class LadderRow:
    ticker: str
    strike: float
    subtitle: str
    p_model: float
    sens: float
    ob: OrderbookStats
    ev_yes: Optional[float]
    ev_no: Optional[float]
    rec: str
    rec_note: str


def _best_bid(levels: list) -> Optional[Tuple[int, float]]:
    if not levels:
        return None
    p, q = max(levels, key=lambda x: x[0])
    return int(p), float(q)


def parse_orderbook_stats(ob_json: dict, depth_window_cents: int = 2) -> OrderbookStats:
    """
    Convert Kalshi orderbook JSON into OrderbookStats.
    """
    ob = ob_json.get("orderbook", {}) or {}
    yes = ob.get("yes") or []
    no = ob.get("no") or []

    y = _best_bid(yes)
    n = _best_bid(no)

    ybid = y[0] if y else None
    yqty = y[1] if y else None
    nbid = n[0] if n else None
    nqty = n[1] if n else None

    ybuy = (100 - nbid) if nbid is not None else None
    nbuy = (100 - ybid) if ybid is not None else None

    spread_y = (ybuy - ybid) if (ybuy is not None and ybid is not None) else None
    spread_n = (nbuy - nbid) if (nbuy is not None and nbid is not None) else None

    def depth_within(levels: list, best_bid: Optional[int]) -> float:
        if not levels or best_bid is None:
            return 0.0
        cutoff = best_bid - depth_window_cents
        return float(sum(float(q) for p, q in levels if int(p) >= cutoff))

    note_parts = []
    if ybid is None:
        note_parts.append("missing_yes_bid")
    if nbid is None:
        note_parts.append("missing_no_bid")

    return OrderbookStats(
        ybid=ybid, yqty=yqty,
        nbid=nbid, nqty=nqty,
        ybuy=ybuy, nbuy=nbuy,
        spread_y=spread_y, spread_n=spread_n,
        depth_y=depth_within(yes, ybid),
        depth_n=depth_within(no, nbid),
        levels_y=len(yes),
        levels_n=len(no),
        note=",".join(note_parts),
    )


def ev_buy_binary(p_win: float, buy_cents: Optional[int], fee_cents: int) -> Optional[float]:
    """
    Buy-only EV for a $1 binary:
      EV = p_win*1 - (price + fee)
    where price/fee are in dollars.
    """
    if buy_cents is None:
        return None
    cost = (buy_cents + fee_cents) / 100.0
    return p_win - cost


def pick_markets_near_spot(markets: List[dict], spot: float, max_strikes: int, band_pct: float) -> List[Tuple[str, float, str]]:
    """
    Pick up to max_strikes from ladder, preferring those within +/- band_pct of spot.
    Returns tuples: (ticker, strike, subtitle)
    """
    prep: List[Tuple[str, float, str]] = []
    for m in markets:
        tkr = m.get("ticker") or m.get("market_ticker")
        strike = market_strike_from_floor(m)
        if not isinstance(tkr, str) or strike is None:
            continue
        subtitle = m.get("subtitle") or m.get("title") or ""
        prep.append((tkr, float(strike), subtitle))

    if not prep:
        return []

    prep_sorted = sorted(prep, key=lambda t: abs(t[1] - spot))

    # When we're intentionally limiting to a small number of strikes, make it *strictly*
    # the closest-to-spot strikes (no extra band heuristics).
    if int(max_strikes) <= 10:
        return prep_sorted[:max_strikes]

    lo = spot * (1.0 - band_pct / 100.0)
    hi = spot * (1.0 + band_pct / 100.0)
    in_band = [t for t in prep_sorted if lo <= t[1] <= hi]

    # If band is too strict, fall back to closest overall
    if len(in_band) >= max(10, max_strikes // 3):
        return in_band[:max_strikes]
    return prep_sorted[:max_strikes]


def evaluate_ladder(
    http: HttpClient,
    markets: List[dict],
    spot: float,
    sigma_blend: float,
    minutes_left: float,
    max_strikes: int,
    band_pct: float,
    fee_cents: int,
    depth_window_cents: int,
    sort_mode: str,
    threads: int,
) -> List[LadderRow]:
    """
    Main ladder evaluation routine.
    """
    chosen = pick_markets_near_spot(markets, spot, max_strikes=max_strikes, band_pct=band_pct)

    def fetch_one(tkr: str) -> Tuple[str, dict]:
        return tkr, get_orderbook(http, tkr)

    ob_map: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(fetch_one, tkr): tkr for (tkr, _, _) in chosen}
        for fut in as_completed(futs):
            tkr = futs[fut]
            try:
                tkr2, ob = fut.result()
                ob_map[tkr2] = ob
            except Exception as e:
                print(f"[WARN] orderbook fetch failed for {tkr}: {e}")

    rows: List[LadderRow] = []
    for (tkr, strike, subtitle) in chosen:
        ob_json = ob_map.get(tkr)
        if not ob_json:
            continue

        p = clamp01(lognormal_prob_above(spot, strike, sigma_blend, minutes_left))
        sens = p * (1.0 - p)
        ob_stats = parse_orderbook_stats(ob_json, depth_window_cents=depth_window_cents)

        ev_yes = ev_buy_binary(p, ob_stats.ybuy, fee_cents)
        ev_no = ev_buy_binary(1.0 - p, ob_stats.nbuy, fee_cents)

        rec = "No trade"
        rec_note = "no positive EV after fees"
        best_side = None
        best_ev = None

        if ev_yes is not None:
            best_side, best_ev = "YES", ev_yes
        if ev_no is not None and (best_ev is None or ev_no > best_ev):
            best_side, best_ev = "NO", ev_no

        if best_ev is not None and best_ev > 0:
            rec = f"Buy {best_side}"
            rec_note = "positive model EV (buy-only)"

        if ob_stats.ybuy is None and ob_stats.nbuy is None:
            rec = "No trade"
            rec_note = "thin book (no reciprocal bid => cannot infer buy-now price)"

        rows.append(LadderRow(
            ticker=tkr,
            strike=strike,
            subtitle=subtitle,
            p_model=p,
            sens=sens,
            ob=ob_stats,
            ev_yes=ev_yes,
            ev_no=ev_no,
            rec=rec,
            rec_note=rec_note,
        ))

    # sorting
    if sort_mode == "strike":
        rows.sort(key=lambda r: r.strike)
    elif sort_mode == "sens":
        rows.sort(key=lambda r: abs(r.p_model - 0.5))
    else:
        def best_ev_val(r: LadderRow) -> float:
            vals = [v for v in (r.ev_yes, r.ev_no) if v is not None]
            return max(vals) if vals else -999.0
        rows.sort(key=best_ev_val, reverse=True)

    return rows