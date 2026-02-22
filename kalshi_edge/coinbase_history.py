"""
Coinbase BTC-USD 1-minute historical candle fetcher for backtests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from kalshi_edge.constants import COINBASE


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_coinbase_candles_1m(
    http: Any,
    start_ts: int,
    end_ts: int,
    product: str = "BTC-USD",
) -> List[dict]:
    """
    Fetch Coinbase 1-minute candles in chunks.

    Returns rows:
      {"minute_end_ts": int, "close": float}
    """
    start_ts = int(start_ts)
    end_ts = int(end_ts)
    if end_ts <= start_ts:
        return []

    # Coinbase commonly limits candle rows per request; keep room below hard max.
    chunk_minutes = 280
    step = chunk_minutes * 60

    by_end_ts: Dict[int, float] = {}
    cur = start_ts
    while cur < end_ts:
        nxt = min(end_ts, cur + step)
        params = {
            "granularity": 60,
            "start": _iso_utc(cur),
            "end": _iso_utc(nxt),
        }
        rows = http.get_json(f"{COINBASE}/products/{product}/candles", params=params)
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, list) or len(r) < 5:
                    continue
                try:
                    ts = int(r[0])
                    close = float(r[4])
                except Exception:
                    continue
                end_bucket = ts + 60
                if start_ts <= end_bucket <= end_ts:
                    by_end_ts[end_bucket] = close
        cur = nxt

    out = [{"minute_end_ts": k, "close": by_end_ts[k]} for k in sorted(by_end_ts.keys())]
    return out


def build_close_by_minute_ts(candles: List[dict]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for r in candles:
        try:
            ts = int(r["minute_end_ts"])
            px = float(r["close"])
        except Exception:
            continue
        out[ts] = px
    return out
