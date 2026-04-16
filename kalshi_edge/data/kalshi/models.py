"""
data/kalshi/models.py — Kalshi market-data parsing helpers.

Interprets Kalshi event/market JSON structures:
  - ABOVE ladder detection (is_above_market, above_markets_from_event)
  - Strike extraction from floor_strike (market_strike_from_floor)
  - URL-to-ticker parsing (event_ticker_from_url)
  - minutes_left computation from close_time

All functions are also re-exported from kalshi_api.py (backward-compat shim).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from kalshi_edge.util.time import parse_iso8601, utc_now


def event_ticker_from_url(url: str) -> str:
    """Extract the event ticker from a Kalshi market URL."""
    s = url.strip().rstrip("/")
    return s.split("/")[-1].upper()


def is_above_market(market: dict) -> bool:
    """
    In the KXBTCD ladder, ABOVE markets are the threshold markets with "-T" in ticker
    and a floor_strike field.
    For KXBTC15M, it's a single market with a floor_strike field.
    """
    tkr = market.get("ticker") or market.get("market_ticker")
    if not isinstance(tkr, str):
        return False
    tkr_up = tkr.upper()

    has_floor = market.get("floor_strike") is not None
    is_kxbtcd_above = "-T" in tkr_up
    is_kxbtc15m = tkr_up.startswith("KXBTC15M-")

    return has_floor and (is_kxbtcd_above or is_kxbtc15m)


def above_markets_from_event(event_json: dict) -> Tuple[str, List[dict], float]:
    """
    Extract ABOVE ladder markets and compute minutes_left from their close_time.

    Returns:
      (event_title, above_markets, minutes_left)
    """
    event = event_json.get("event") or {}
    title = event.get("title", "(no title)")
    nested = event_json.get("markets") or event.get("markets") or []

    above = [m for m in nested if is_above_market(m)]
    if not above:
        return title, [], 0.0

    close_times = []
    for m in above:
        ct = m.get("close_time")
        if isinstance(ct, str):
            close_times.append(parse_iso8601(ct))

    now = utc_now()
    close_time = min(close_times) if close_times else now
    minutes_left = max(0.0, (close_time - now).total_seconds() / 60.0)

    return title, above, minutes_left


def market_strike_from_floor(market: dict) -> Optional[float]:
    """
    In Kalshi KXBTCD ladders the threshold is represented by floor_strike=59999.99
    which corresponds to "$60,000 or above" (strike_type="greater").
    In KXBTC15M, the threshold is exact (strike_type="greater_or_equal").
    """
    fs = market.get("floor_strike")
    if fs is None:
        return None
    try:
        fs_val = float(fs)
        strike_type = market.get("strike_type")
        if strike_type == "greater":
            return round(fs_val + 0.01, 2)
        elif strike_type == "greater_or_equal":
            return round(fs_val, 2)
        else:
            # Conservative fallback for legacy/partial payloads: use exact threshold.
            return round(fs_val, 2)
    except Exception:
        return None
