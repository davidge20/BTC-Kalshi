"""
market_discovery.py

@brief 
Auto-discovery:
If the user doesn't supply `--event` or `--url`, pick the soonest-closing BTC
Above/Below event by scanning Kalshi `/markets` for markets whose
`event_ticker` starts with `KXBTCD-`.

Currently, we are focusing on the `KXBTCD-` market
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from kalshi_edge.constants import KALSHI
from kalshi_edge.http_client import HttpClient
from kalshi_edge.util.time import parse_iso8601, utc_now


@dataclass
class DiscoveredEvent:
    event_ticker: str
    market_title: str
    close_time: str
    minutes_left: float


def _list_markets_closing_soon(
    http: HttpClient,
    min_close_ts: int,
    max_close_ts: int,
    limit: int = 1000,
    max_pages: int = 10,
) -> List[dict]:
    """
    Pull markets closing in [min_close_ts, max_close_ts], handling cursor pagination.

    Notes:
    - Responses typically include `markets` and optionally a pagination cursor.
    """
    all_markets: List[dict] = []
    cursor: Optional[str] = None

    for page in range(1, max_pages + 1):
        params: dict = {"limit": limit, "min_close_ts": min_close_ts, "max_close_ts": max_close_ts}
        if cursor:
            params["cursor"] = cursor

        data = http.get_json(f"{KALSHI}/markets", params=params)

        markets = data.get("markets") or []
        all_markets.extend(markets)

        cursor = data.get("cursor") or data.get("next_cursor")
        if http.debug:
            cdisp = "(none)" if not cursor else (cursor[:32] + "...")
            print(f"[Kalshi] page={page} markets={len(markets)} cursor={cdisp}")

        if not cursor:
            break

    return all_markets


def _get_str(m: dict, *keys: str) -> str:
    """Return first present key as string, else ''."""
    for k in keys:
        v = m.get(k)
        if isinstance(v, str):
            return v
    return ""


def _is_kxbtcd_event(event_ticker: str) -> bool:
    """Return True if this looks like a BTC Above/Below event ticker."""
    return event_ticker.upper().startswith("KXBTCD-")


def discover_current_event(
    window_minutes: int = 70,
    debug_http: bool = False,
) -> DiscoveredEvent:
    """
    Discover the soonest-closing BTC Above/Below event by scanning markets closing soon.

    Strategy:
    - pull closing-soon markets via /markets
    - keep those that:
        * have an event_ticker that looks like KXBTCD-...
        * have a close_time
    - choose the soonest close_time
    """
    http = HttpClient(debug=debug_http)
    now = utc_now()

    min_close_ts = int(now.timestamp())
    max_close_ts = int(now.timestamp() + window_minutes * 60)

    markets = _list_markets_closing_soon(http, min_close_ts, max_close_ts)
    if debug_http:
        print(f"[Discover] fetched markets={len(markets)} window_minutes={window_minutes}")

    candidates: List[dict] = []

    for m in markets:
        # Field name differences happen; try common ones.
        event_ticker = _get_str(m, "event_ticker", "eventTicker", "event")
        title = _get_str(m, "title", "market_title", "name")
        close_time = _get_str(m, "close_time", "closeTime")

        if not event_ticker or not _is_kxbtcd_event(event_ticker):
            continue
        if not close_time:
            continue

        candidates.append(m)

    if not candidates:
        raise RuntimeError(
            "Could not auto-discover a KXBTCD event from /markets. "
            "Try widening --window-minutes (e.g. 360 or 1440), or pass --event/--url."
        )

    # choose soonest-closing
    def close_dt(m: dict) -> datetime:
        return parse_iso8601(_get_str(m, "close_time", "closeTime"))

    chosen = min(candidates, key=close_dt)

    event_ticker = _get_str(chosen, "event_ticker", "eventTicker", "event").upper()
    title = _get_str(chosen, "title", "market_title", "name")
    close_time = _get_str(chosen, "close_time", "closeTime")

    minutes_left = max(0.0, (close_dt(chosen) - now).total_seconds() / 60.0)

    if debug_http:
        print(f"[Discover] selected event={event_ticker} close_time={close_time} minutes_left={minutes_left:.2f}")
        print(f"[Discover] title={title}")

    return DiscoveredEvent(
        event_ticker=event_ticker,
        market_title=title,
        close_time=close_time,
        minutes_left=minutes_left,
    )