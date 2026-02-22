"""
Kalshi event/market/candlestick access for backtesting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from kalshi_edge.constants import KALSHI
from kalshi_edge.util.time import parse_iso8601


def _first_present(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d:
            return d.get(k)
    return None


def parse_price_cents(raw: Any) -> Optional[int]:
    """
    Normalize heterogeneous API price fields into integer cents in [0, 100].

    Supports:
    - integer cents (52)
    - decimal dollars/probabilities as strings ("0.52")
    - numeric strings ("52")
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None

    val: float
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        s = raw.strip().replace("%", "")
        if not s:
            return None
        try:
            val = float(s)
        except Exception:
            return None
    else:
        return None

    if val < 0:
        return None
    if val <= 1.0:
        cents = int(round(val * 100.0))
    else:
        cents = int(round(val))

    if 0 <= cents <= 100:
        return cents
    return None


def _parse_ts_to_epoch(v: Any) -> Optional[int]:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            if s.isdigit():
                return int(s)
            return int(parse_iso8601(s).timestamp())
        except Exception:
            return None
    return None


def normalize_candles(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = _parse_ts_to_epoch(
            _first_present(r, ["end_period_ts", "end_ts", "close_ts", "period_end_ts", "ts", "time"])
        )
        if ts is None:
            continue

        ybid = parse_price_cents(
            _first_present(r, ["yes_bid_cents", "yes_bid", "yes_bid_price", "bid_yes", "ybid"])
        )
        yask = parse_price_cents(
            _first_present(r, ["yes_ask_cents", "yes_ask", "yes_ask_price", "ask_yes", "yask"])
        )

        # Some variants expose only a single yes close/price field.
        if ybid is None and yask is None:
            px = parse_price_cents(
                _first_present(r, ["yes_close_cents", "yes_close", "close", "price", "close_price"])
            )
            if px is not None:
                ybid = px
                yask = px

        out.append({"ts": int(ts), "yes_bid_cents": ybid, "yes_ask_cents": yask})

    out.sort(key=lambda x: int(x["ts"]))
    return out


def get_historical_cutoff(http: Any) -> datetime:
    data = http.get_json(f"{KALSHI}/historical/cutoff")
    cand = None
    if isinstance(data, dict):
        cand = (
            data.get("cutoff")
            or data.get("historical_cutoff")
            or (data.get("historical") or {}).get("cutoff")
        )
    if isinstance(cand, str):
        return parse_iso8601(cand)
    raise RuntimeError("Could not parse /historical/cutoff response")


def list_events(
    http: Any,
    series_ticker: str,
    start_ts: int,
    end_ts: int,
    status: str = "settled",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    for _ in range(200):
        params: Dict[str, Any] = {
            "limit": 200,
            "series_ticker": str(series_ticker),
            "status": str(status),
        }
        if cursor:
            params["cursor"] = cursor
        data = http.get_json(f"{KALSHI}/events", params=params)
        rows = []
        if isinstance(data, dict):
            rows = data.get("events") or []
            cursor = data.get("cursor") or data.get("next_cursor")
        if not isinstance(rows, list):
            rows = []

        for e in rows:
            if not isinstance(e, dict):
                continue
            close_s = _first_present(e, ["close_time", "closeTime", "event_close_time"])
            try:
                close_dt = parse_iso8601(str(close_s))
            except Exception:
                continue
            ts = int(close_dt.timestamp())
            if int(start_ts) <= ts < int(end_ts):
                out.append(e)

        if not cursor:
            break

    out.sort(key=lambda e: str(_first_present(e, ["close_time", "closeTime"]) or ""))
    return out


def _markets_from_payload(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    rows = data.get("markets") or data.get("event", {}).get("markets") or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def list_markets_for_event(http: Any, event_ticker: str, cutoff_dt: datetime) -> List[Dict[str, Any]]:
    """
    Return event markets. Prefer live endpoint and fallback to historical.
    """
    params = {"event_ticker": str(event_ticker), "limit": 500}

    try:
        live = http.get_json(f"{KALSHI}/markets", params=params)
        rows = _markets_from_payload(live)
        if rows:
            return rows
    except Exception:
        pass

    hist = http.get_json(f"{KALSHI}/historical/markets", params=params)
    rows = _markets_from_payload(hist)
    if rows:
        return rows

    # Final fallback: event detail endpoint(s).
    try:
        ev = http.get_json(f"{KALSHI}/events/{event_ticker}", params={"with_nested_markets": "true"})
        rows = _markets_from_payload(ev)
        if rows:
            return rows
    except Exception:
        pass

    evh = http.get_json(f"{KALSHI}/historical/events/{event_ticker}", params={"with_nested_markets": "true"})
    return _markets_from_payload(evh)


def _extract_candle_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("candlesticks", "candles", "data"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def fetch_market_candles_1m(
    http: Any,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    *,
    use_historical: bool = False,
) -> List[Dict[str, Any]]:
    """
    Fetch 1-minute candles for one market and normalize to:
      {"ts": int, "yes_bid_cents": Optional[int], "yes_ask_cents": Optional[int]}
    """
    params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": 1}
    live_url = f"{KALSHI}/markets/{market_ticker}/candlesticks"
    hist_url = f"{KALSHI}/historical/markets/{market_ticker}/candlesticks"
    order = [hist_url, live_url] if use_historical else [live_url, hist_url]

    last_err: Optional[Exception] = None
    for url in order:
        try:
            payload = http.get_json(url, params=params)
            rows = _extract_candle_rows(payload)
            return normalize_candles(rows)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Failed candles fetch for {market_ticker}: {last_err}")


def fetch_batch_market_candles_1m(
    http: Any,
    market_tickers: List[str],
    start_ts: int,
    end_ts: int,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Best-effort batch fetch path for live-tier markets.
    Falls back to empty dict if endpoint/shape is unavailable.
    """
    tickers = [str(t).strip() for t in market_tickers if str(t).strip()]
    if not tickers:
        return {}

    params = {
        "tickers": ",".join(tickers[:100]),
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "period_interval": 1,
    }
    urls = [
        f"{KALSHI}/markets/candlesticks",
        f"{KALSHI}/candlesticks",
    ]
    for url in urls:
        try:
            payload = http.get_json(url, params=params)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("candlesticks_by_ticker") or payload.get("candles_by_ticker") or {}
        if not isinstance(raw, dict):
            continue
        out: Dict[str, List[Dict[str, Any]]] = {}
        for tkr, rows in raw.items():
            if isinstance(rows, list):
                out[str(tkr)] = normalize_candles([x for x in rows if isinstance(x, dict)])
        if out:
            return out
    return {}
