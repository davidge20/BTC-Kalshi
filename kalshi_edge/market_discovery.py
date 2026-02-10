# kalshi_edge/market_discovery.py

"""
market_discovery.py

Auto-discovery ("Option A"):
Find the BTC ABOVE/BELOW event that is closing soon, without needing a URL.

This version is deliberately *robust*:
- Kalshi /markets payloads can vary a bit (fields may be missing or named differently)
- We print useful samples when discovery fails
- We use timezone-aware UTC consistently
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from kalshi_edge.constants import KALSHI
from kalshi_edge.http_client import HttpClient


@dataclass
class DiscoveredEvent:
    event_ticker: str
    market_title: str
    close_time: str
    minutes_left: float


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso8601(s: str) -> datetime:
    """Kalshi uses a Z suffix; normalize to Python's +00:00 format."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


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
    - Some APIs return "cursor", some return "next_cursor".
    - Some return the market list under "markets"; we handle that.
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


def _looks_like_btc_abovebelow_family(event_ticker: str) -> bool:
    # Your observed family: KXBTCD-...
    return event_ticker.upper().startswith("KXBTCD-")


def discover_current_event(
    window_minutes: int = 70,
    debug_http: bool = False,
    # Be less strict than "bitcoin price" because titles vary.
    title_keywords: Tuple[str, ...] = ("bitcoin", "btc", "above", "below"),
) -> DiscoveredEvent:
    """
    Discover the soonest-closing BTC Above/Below event by scanning markets closing soon.

    Strategy:
    - pull closing-soon markets via /markets
    - keep those that:
        * have an event_ticker that looks like KXBTCD-...
        * have a close_time
        * have a title that mentions something BTC-ish
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

        if not event_ticker or not _looks_like_btc_abovebelow_family(event_ticker):
            continue
        if not close_time:
            continue

        # Title keyword check (loose)
        tlow = title.lower()
        if title and not any(kw in tlow for kw in title_keywords):
            # If title is empty, don't filter it out — some payloads omit it.
            continue

        candidates.append(m)

    if not candidates:
        # Print a useful sample so you can see what fields you actually got.
        print("\n[Discover] No candidates found. Showing samples from /markets response:\n")

        # show first 25 markets with close_time
        shown = 0
        for m in markets:
            ct = _get_str(m, "close_time", "closeTime")
            if not ct:
                continue
            et = _get_str(m, "event_ticker", "eventTicker", "event")
            t = _get_str(m, "title", "market_title", "name")
            cat = _get_str(m, "category", "Category")
            print(f"- close_time={ct} category={cat} event_ticker={et} title={t[:90]}")
            shown += 1
            if shown >= 25:
                break

        raise RuntimeError(
            "Could not auto-discover a KXBTCD event from /markets. "
            "Try widening --window-minutes (e.g. 360 or 1440), "
            "or pass --event manually."
        )

    # choose soonest-closing
    def close_dt(m: dict) -> datetime:
        return _parse_iso8601(_get_str(m, "close_time", "closeTime"))

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