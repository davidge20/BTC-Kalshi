"""
Coinbase BTC-USD 1-minute historical candle fetcher for backtests.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from kalshi_edge.constants import COINBASE


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _status_code_from_exc(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return int(code) if isinstance(code, int) else None


def _retry_after_seconds(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except Exception:
        return None


def _get_coinbase_json_with_retry(
    http: Any,
    url: str,
    *,
    params: Dict[str, Any],
    max_attempts: int = 6,
    base_sleep_seconds: float = 0.5,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, int(max_attempts) + 1):
        try:
            return http.get_json(url, params=params)
        except Exception as exc:
            last_exc = exc
            code = _status_code_from_exc(exc)
            if code not in {429, 500, 502, 503, 504}:
                raise
            if attempt >= int(max_attempts):
                break
            retry_after = _retry_after_seconds(exc)
            sleep_s = (
                float(retry_after)
                if retry_after is not None
                else min(8.0, float(base_sleep_seconds) * (2.0 ** (attempt - 1)))
            )
            time.sleep(max(0.05, sleep_s))
    if last_exc is not None:
        raise last_exc
    return []


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
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if end_ts > now_ts:
        end_ts = now_ts
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
        rows = _get_coinbase_json_with_retry(http, f"{COINBASE}/products/{product}/candles", params=params)
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
