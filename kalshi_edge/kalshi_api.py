"""Kalshi API helpers for ladder market selection.

@brief
- Identify the KXBTCD ABOVE threshold contracts from an event's nested markets.
- Derive trade-ready metadata from Kalshi's ladder encoding (time remaining, strike).
- Provide access to market orderbooks and authenticated portfolio endpoints.
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any
from typing import Protocol
from kalshi_edge.constants import KALSHI
from kalshi_edge.formatting import parse_iso8601, utc_now
from kalshi_edge.http_client import HttpClient

class KalshiAuthLike(Protocol):
    """Structural type for objects that can generate Kalshi auth headers."""
    def headers(self, method: str, path: str, timestamp_ms: Optional[str] = None) -> Dict[str, str]: ...

def event_ticker_from_url(url: str) -> str:
    """Extract the event ticker from a Kalshi market URL."""
    s = url.strip().rstrip("/")
    return s.split("/")[-1].upper()


def get_event(http: HttpClient, event_ticker: str) -> Dict[str, Any]:
    """Fetch an event including nested markets."""
    return http.get_json(f"{KALSHI}/events/{event_ticker}", params={"with_nested_markets": "true"})


def get_orderbook(http: HttpClient, market_ticker: str) -> Dict[str, Any]:
    """Fetch a market's orderbook (YES bids + NO bids)."""
    return http.get_json(f"{KALSHI}/markets/{market_ticker}/orderbook")

def create_order(
    http: HttpClient,
    auth: KalshiAuthLike,
    order_data: Dict[str, Any],
    *,
    base_url: str = KALSHI,
) -> Dict[str, Any]:
    """Create an order (authenticated)."""
    path = "/trade-api/v2/portfolio/orders"
    headers = auth.headers("POST", path)
    headers["Content-Type"] = "application/json"
    return http.post_json(f"{base_url}/portfolio/orders", json_body=order_data, headers=headers)


def get_positions(
    http: HttpClient,
    auth: KalshiAuthLike,
    *,
    base_url: str = KALSHI,
    ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    limit: int = 1000,
    cursor: Optional[str] = None,
    count_filter: Optional[str] = "position",
    subaccount: Optional[int] = None,
) -> Dict[str, Any]:
    """Get portfolio positions (authenticated)."""
    path = "/trade-api/v2/portfolio/positions"
    headers = auth.headers("GET", path)
    params: Dict[str, Any] = {"limit": int(limit)}
    if ticker:
        params["ticker"] = ticker
    if event_ticker:
        params["event_ticker"] = event_ticker
    if cursor:
        params["cursor"] = cursor
    if count_filter:
        params["count_filter"] = count_filter
    if subaccount is not None:
        params["subaccount"] = int(subaccount)
    return http.get_json(f"{base_url}/portfolio/positions", params=params, headers=headers)


def is_above_market(market: dict) -> bool:
    """
    In the KXBTCD ladder, ABOVE markets are the threshold markets with "-T" in ticker
    and a floor_strike field.
    """
    tkr = market.get("ticker") or market.get("market_ticker")
    return isinstance(tkr, str) and ("-T" in tkr) and (market.get("floor_strike") is not None)


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
    # Ladders can contain multiple close times; treat the *earliest* as the
    # effective deadline so downstream logic doesn't assume extra time that isn't there.
    close_time = min(close_times) if close_times else now
    minutes_left = max(0.0, (close_time - now).total_seconds() / 60.0)

    return title, above, minutes_left


def market_strike_from_floor(market: dict) -> Optional[float]:
    """
    In Kalshi KXBTCD ladders the threshold is represented by floor_strike=59999.99
    which corresponds to "$60,000 or above".
    """
    fs = market.get("floor_strike")
    if fs is None:
        return None
    try:
        return round(float(fs) + 0.01, 2)
    except Exception:
        return None
