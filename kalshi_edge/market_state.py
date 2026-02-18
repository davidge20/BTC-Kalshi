"""
market_state.py

@brief 
Build MarketState:
- spot price (Deribit index)
- implied volatility estimate (Deribit options, near ATM)
- realized short-term volatility (Coinbase 1-min candles)
- blend implied & realized into sigma_blend
- compute "confidence" label and expected 1-sigma move over time left

This module is intentionally independent from Kalshi.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.constants import DERIBIT, COINBASE, MINUTES_PER_YEAR
from kalshi_edge.formatting import fmt_money
from kalshi_edge.http_client import HttpClient
from kalshi_edge.math_models import expected_one_sigma_move_pct


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MarketState:
    ts_utc: datetime
    minutes_left: float
    spot: float
    sigma_implied: float
    sigma_realized: float
    sigma_blend: float
    confidence: str
    one_sigma_move_pct: float
    note: str


def deribit_index_price(http: HttpClient, index_name: str = "btc_usd") -> float:
    data = http.get_json(f"{DERIBIT}/public/get_index_price", params={"index_name": index_name})
    px = float(data["result"]["index_price"])
    return px


def normalize_mark_iv(x: Any) -> Optional[float]:
    """
    Deribit `mark_iv` is treated as a percent (e.g. 84.9 means 84.9%).
    Normalize to a decimal volatility (0.849).
    """
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if v <= 0:
        return None
    return v / 100.0


def parse_deribit_instrument_name(name: str) -> Optional[Tuple[datetime, float]]:
    """
    Deribit option instrument format: BTC-20FEB26-64000-C
    We parse expiry date and strike. Expiry time is approximated as 08:00 UTC.
    """
    try:
        parts = name.split("-")
        date_str = parts[1]
        strike = float(parts[2])
        dt = datetime.strptime(date_str, "%d%b%y").replace(tzinfo=timezone.utc)
        dt = dt.replace(hour=8, minute=0, second=0, microsecond=0)
        return dt, strike
    except Exception:
        return None


def deribit_atm_implied_vol(http: HttpClient, spot: float, band_pct: float = 3.0, max_expiries_scan: int = 20) -> Tuple[float, str]:
    """
    Estimate implied vol by:
      - pulling Deribit option summaries
      - filtering strikes within +/- band_pct of spot
      - grouping by expiry
      - picking the nearest expiry with >=4 IV samples
      - taking the median IV
    """
    data = http.get_json(
        f"{DERIBIT}/public/get_book_summary_by_currency",
        params={"currency": "BTC", "kind": "option"},
    )
    rows = data["result"]

    band = spot * (band_pct / 100.0)
    lo, hi = spot - band, spot + band

    now = utc_now()
    near: List[Tuple[datetime, float]] = []

    for r in rows:
        name = r.get("instrument_name")
        parsed = parse_deribit_instrument_name(name) if isinstance(name, str) else None
        if not parsed:
            continue
        expiry, strike = parsed
        if expiry <= now:
            continue
        if not (lo <= strike <= hi):
            continue

        iv = normalize_mark_iv(r.get("mark_iv"))
        if iv is None or iv <= 0:
            continue
        near.append((expiry, iv))

    if not near:
        raise RuntimeError("No near-ATM Deribit options found. Try widening --iv-band-pct.")

    by_exp: Dict[datetime, List[float]] = {}
    for expiry, iv in near:
        by_exp.setdefault(expiry, []).append(iv)

    expiries = sorted(by_exp.keys())
    for exp in expiries[:max_expiries_scan]:
        ivs = by_exp[exp]
        if len(ivs) >= 4:
            med = float(statistics.median(ivs))
            note = f"nearest_expiry={exp.isoformat()} ivs_used={len(ivs)} band=±{band_pct}%"
            return med, note

    exp0 = expiries[0]
    med0 = float(statistics.median(by_exp[exp0]))
    note = f"fallback_expiry={exp0.isoformat()} ivs_used={len(by_exp[exp0])} band=±{band_pct}%"
    return med0, note


def coinbase_realized_vol_1h(http: HttpClient, product: str = "BTC-USD", minutes: int = 61) -> float:
    """
    Fetch last N 1-minute candles and compute annualized realized vol.

    Steps:
      - compute 1-minute log returns
      - take stdev
      - annualize by sqrt(minutes/year)
    """
    candles = http.get_json(f"{COINBASE}/products/{product}/candles", params={"granularity": 60})
    if not isinstance(candles, list) or len(candles) < minutes:
        raise RuntimeError("Coinbase candles insufficient.")

    candles = sorted(candles, key=lambda x: x[0])
    closes = [float(c[4]) for c in candles[-minutes:]]

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    stdev = statistics.pstdev(rets) if len(rets) > 1 else 0.0

    vol_ann = stdev * math.sqrt(MINUTES_PER_YEAR)
    return vol_ann


def blend_vol(implied: float, realized: float) -> Tuple[float, str]:
    """
    A simple explainable blend rule.

    If realized is much larger than implied, we split 50/50.
    Otherwise we trust implied more and blend 70/30.
    """
    if implied <= 0:
        return realized, "implied<=0 so use realized"

    ratio = realized / implied
    if ratio > 1.5:
        return 0.5 * implied + 0.5 * realized, f"realized/implied={ratio:.2f} -> 50/50"
    return 0.7 * implied + 0.3 * realized, f"realized/implied={ratio:.2f} -> 70/30"


def confidence_label(implied: float, realized: float) -> str:
    """Crude confidence based on disagreement between implied and realized."""
    if implied <= 0:
        return "Low"
    diff = abs(realized - implied) / implied
    if diff <= 0.35:
        return "High"
    if diff <= 0.65:
        return "Medium"
    return "Low"


def build_market_state(http: HttpClient, minutes_left: float, iv_band_pct: float = 3.0) -> MarketState:
    """
    Compute MarketState given minutes_left until Kalshi close.
    """
    ts = utc_now()

    spot = deribit_index_price(http, "btc_usd")
    sigma_imp, note = deribit_atm_implied_vol(http, spot, band_pct=iv_band_pct)
    sigma_real = coinbase_realized_vol_1h(http, "BTC-USD", minutes=61)

    sigma_bl, blend_note = blend_vol(sigma_imp, sigma_real)
    conf = confidence_label(sigma_imp, sigma_real)
    one_sigma = expected_one_sigma_move_pct(sigma_bl, minutes_left)

    note2 = f"{note}; {blend_note}"

    return MarketState(
        ts_utc=ts,
        minutes_left=minutes_left,
        spot=spot,
        sigma_implied=sigma_imp,
        sigma_realized=sigma_real,
        sigma_blend=sigma_bl,
        confidence=conf,
        one_sigma_move_pct=one_sigma,
        note=note2,
    )