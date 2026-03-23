"""
market_state.py — build MarketState from external BTC venues.

Gathers:
- Spot price (Deribit index)
- Deribit DVOL index (forward-looking implied vol)
- Implied volatility estimate (Deribit options, near ATM)
- Realized short-term volatility (Coinbase 1-min candles)
- GARCH(1,1) forecast volatility
- Regression-adjusted sigma (encompassing DVOL + RV blend)
- Confidence label and expected 1-sigma move over time remaining

This module is intentionally independent from Kalshi — it only talks to
Deribit and Coinbase to build inputs for the probability model.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from kalshi_edge.constants import DERIBIT, COINBASE, MINUTES_PER_YEAR
from kalshi_edge.http_client import HttpClient
from kalshi_edge.math_models import expected_one_sigma_move_pct
from kalshi_edge.util.time import utc_now  # canonical source

_log = logging.getLogger(__name__)


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
    sigma_garch: float = 0.0
    dvol_current: float = 0.0
    sigma_adjusted: float = 0.0


def deribit_index_price(http: HttpClient, index_name: str = "btc_usd") -> float:
    data = http.get_json(f"{DERIBIT}/public/get_index_price", params={"index_name": index_name})
    px = float(data["result"]["index_price"])
    return px


def fetch_deribit_dvol(http: HttpClient, currency: str = "BTC") -> float:
    """
    Fetch the current Deribit DVOL index as an annualized decimal.

    DVOL is reported as a percentage (e.g. 60 ⇒ 60% annual vol);
    we normalize to decimal (0.60).
    """
    now_ms = int(utc_now().timestamp() * 1000)
    start_ms = now_ms - 7_200_000  # 2 h lookback to guarantee ≥1 point

    data = http.get_json(
        f"{DERIBIT}/public/get_volatility_index_data",
        params={
            "currency": currency,
            "start_timestamp": start_ms,
            "end_timestamp": now_ms,
            "resolution": 60,
        },
    )
    rows = data.get("result", {}).get("data", [])
    if not rows:
        raise RuntimeError("No DVOL data returned from Deribit")

    latest = rows[-1]
    dvol_raw = float(latest[4])  # close value
    return dvol_raw / 100.0


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


def fetch_coinbase_1min_closes(http: HttpClient, product: str = "BTC-USD") -> List[float]:
    """
    Fetch up to 300 of the most recent 1-minute candles from Coinbase.

    Returns close prices sorted oldest → newest.
    """
    candles = http.get_json(
        f"{COINBASE}/products/{product}/candles",
        params={"granularity": 60},
    )
    if not isinstance(candles, list) or len(candles) < 2:
        raise RuntimeError(
            f"Coinbase candles insufficient: got {len(candles) if isinstance(candles, list) else 0}"
        )
    candles = sorted(candles, key=lambda x: x[0])
    return [float(c[4]) for c in candles]


def log_returns_from_closes(closes: List[float]) -> pd.Series:
    """Compute 1-minute log returns from an ordered list of closes."""
    arr = np.array(closes)
    return pd.Series(np.log(arr[1:] / arr[:-1]))


def realized_vol_from_returns(returns: pd.Series, window: int = 60) -> float:
    """
    Annualized realized vol from the trailing *window* 1-minute returns.

    Uses population stdev and annualizes via sqrt(minutes_per_year).
    """
    tail = returns.iloc[-window:]
    if len(tail) < 2:
        return 0.0
    stdev = float(tail.std(ddof=0))
    return stdev * math.sqrt(MINUTES_PER_YEAR)


def coinbase_realized_vol_1h(http: HttpClient, product: str = "BTC-USD", minutes: int = 61) -> float:
    """
    Fetch last N 1-minute candles and compute annualized realized vol.

    Kept for backward compatibility.  Prefer fetch_coinbase_1min_closes()
    + realized_vol_from_returns() in new code to avoid double-fetching.
    """
    closes = fetch_coinbase_1min_closes(http, product)
    if len(closes) < minutes:
        raise RuntimeError("Coinbase candles insufficient.")
    closes = closes[-minutes:]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    stdev = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    return stdev * math.sqrt(MINUTES_PER_YEAR)


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


def build_market_state(
    http: HttpClient,
    minutes_left: float,
    iv_band_pct: float = 3.0,
    vol_model: object | None = None,
) -> MarketState:
    """
    Compute MarketState given minutes_left until Kalshi close.

    Volatility priority:
      1. Encompassing Regression (adjusted_sigma from DVOL + RV)  ← new
      2. GARCH(1,1) forecast from Coinbase 1-min returns
      3. Heuristic IV/RV blend                                    (fallback)

    Parameters
    ----------
    vol_model : VolatilityRegression | None
        A fitted :class:`~kalshi_edge.vol_regression.VolatilityRegression`.
        When provided (and fitted), its prediction becomes the primary sigma.
    """
    from kalshi_edge.garch import forecast_garch_volatility

    ts = utc_now()

    spot = deribit_index_price(http, "btc_usd")

    # --- Deribit DVOL index ---
    dvol = 0.0
    dvol_note = ""
    try:
        dvol = fetch_deribit_dvol(http)
        dvol_note = f"dvol={dvol*100:.1f}%"
    except Exception as e:
        _log.warning("Deribit DVOL fetch failed (%s)", e)
        dvol_note = f"dvol_failed: {e}"

    # --- Deribit IV (kept for diagnostics) ---
    try:
        sigma_imp, iv_note = deribit_atm_implied_vol(http, spot, band_pct=iv_band_pct)
    except Exception as e:
        _log.warning("Deribit IV fetch failed (%s); continuing without IV", e)
        sigma_imp = 0.0
        iv_note = f"iv_fetch_failed: {e}"

    # --- Coinbase: fetch once, derive both RV and GARCH ---
    closes = fetch_coinbase_1min_closes(http, "BTC-USD")
    returns = log_returns_from_closes(closes)
    sigma_real = realized_vol_from_returns(returns, window=60)

    # --- GARCH(1,1) forecast ---
    sigma_garch = 0.0
    garch_note = ""
    try:
        sigma_garch, garch_note = forecast_garch_volatility(returns)
    except Exception as e:
        _log.warning("GARCH forecast failed (%s); falling back", e)
        garch_note = f"garch_failed: {e}"

    # --- Encompassing Regression (primary when available) ---
    sigma_adj = 0.0
    regression_note = ""
    if vol_model is not None and getattr(vol_model, "is_fitted", False):
        try:
            sigma_adj = vol_model.predict(dvol, sigma_real)
            regression_note = f"regression σ_adj={sigma_adj*100:.1f}%"
        except Exception as e:
            _log.warning("Regression prediction failed (%s); falling back", e)
            regression_note = f"regression_failed: {e}"

    # --- Choose primary sigma ---
    if sigma_adj > 0:
        sigma_primary = sigma_adj
        blend_note = f"primary=regression; {regression_note}"
    elif sigma_garch > 0:
        sigma_primary = sigma_garch
        blend_note = f"primary=garch; {garch_note}"
    else:
        sigma_bl, heuristic_note = blend_vol(sigma_imp, sigma_real)
        sigma_primary = sigma_bl
        blend_note = f"primary=blend_fallback; {heuristic_note}"

    conf = confidence_label(sigma_imp, sigma_real)
    one_sigma = expected_one_sigma_move_pct(sigma_primary, minutes_left)
    full_note = f"{dvol_note}; {iv_note}; {blend_note}"

    return MarketState(
        ts_utc=ts,
        minutes_left=minutes_left,
        spot=spot,
        sigma_implied=sigma_imp,
        sigma_realized=sigma_real,
        sigma_blend=sigma_primary,
        confidence=conf,
        one_sigma_move_pct=one_sigma,
        note=full_note,
        sigma_garch=sigma_garch,
        dvol_current=dvol,
        sigma_adjusted=sigma_adj,
    )
