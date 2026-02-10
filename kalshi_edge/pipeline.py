"""
pipeline.py

High-level "glue" that run.py calls.

This is the only place where we coordinate:
- fetch event
- extract ladder
- compute market state
- evaluate ladder

Keeping glue in one place prevents circular imports.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_api import get_event, above_markets_from_event, event_ticker_from_url
from kalshi_edge.market_state import build_market_state, MarketState
from kalshi_edge.ladder_eval import evaluate_ladder, LadderRow


@dataclass
class EvaluationResult:
    event_ticker: str
    event_title: str
    minutes_left: float
    market_state: MarketState
    rows: List[LadderRow]


def evaluate_event(
    *,
    event: Optional[str] = None,
    url: Optional[str] = None,
    max_strikes: int = 120,
    band_pct: float = 25.0,
    sort: str = "ev",
    fee_cents: int = 1,
    depth_window_cents: int = 2,
    threads: int = 10,
    iv_band_pct: float = 3.0,
    debug_http: bool = False,
) -> EvaluationResult:
    """
    Evaluate a single Kalshi event.
    Supply either:
      - event="KXBTCD-..." OR
      - url="https://kalshi.com/markets/.../kxbtcd-..."
    """
    if not event and not url:
        raise ValueError("Must provide event or url.")

    event_ticker = event.upper() if event else event_ticker_from_url(url or "")
    http = HttpClient(debug=debug_http)

    event_json = get_event(http, event_ticker)
    event_title, above_markets, minutes_left = above_markets_from_event(event_json)
    if not above_markets:
        raise RuntimeError("Event had no ABOVE ladder markets (-T). Confirm you used the ABOVE/BELOW event.")

    ms = build_market_state(http=http, minutes_left=minutes_left, iv_band_pct=iv_band_pct)

    rows = evaluate_ladder(
        http=http,
        markets=above_markets,
        spot=ms.spot,
        sigma_blend=ms.sigma_blend,
        minutes_left=minutes_left,
        max_strikes=max_strikes,
        band_pct=band_pct,
        fee_cents=fee_cents,
        depth_window_cents=depth_window_cents,
        sort_mode=sort,
        threads=threads,
    )

    return EvaluationResult(
        event_ticker=event_ticker,
        event_title=event_title,
        minutes_left=minutes_left,
        market_state=ms,
        rows=rows,
    )