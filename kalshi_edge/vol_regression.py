"""
vol_regression.py — Encompassing Regression for optimal volatility blending.

Uses a weighted Deribit implied-vol proxy together with trailing realized
volatility to produce an adjusted_sigma via OLS regression.

The pipeline (no lookahead bias):
  1. Fetch historical Coinbase 1-min candles → rolling 60-min RV, hourly snapshots
  2. Fetch historical Deribit DVOL hourly snapshots as the historical implied proxy
  3. Align on top-of-hour timestamps, create the weighted implied/RV feature
  4. Fit OLS: Target_RV_Forward ~ β₀ + β₁·IV_Proxy_Current + β₂·RV_Trailing
  5. At runtime, predict adjusted_sigma from live weighted IV/RV + live RV
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from kalshi_edge.constants import DERIBIT, MINUTES_PER_YEAR
from kalshi_edge.http_client import HttpClient
from kalshi_edge.backtesting.coinbase_history import fetch_coinbase_candles_1m
from kalshi_edge.live_iv_cache import read_live_iv_snapshots
from kalshi_edge.market_state import blend_vol, fixed_live_blend

_log = logging.getLogger(__name__)

LEGACY_FEATURE_COL = "DVOL_Current"
FEATURE_COL = "IV_Proxy_Current"
RV_COL = "RV_Trailing"
TARGET_COL = "Target_RV_Forward"

def fetch_deribit_dvol_hourly(
    http: HttpClient,
    start_ts: int,
    end_ts: int,
    currency: str = "BTC",
) -> pd.DataFrame:
    """
    Backtest helper: fetch hourly Deribit DVOL index snapshots.
    """
    start_ms = int(start_ts) * 1000
    end_ms = int(end_ts) * 1000

    all_rows: list[tuple[int, float]] = []
    cursor_ms = start_ms

    while cursor_ms < end_ms:
        data = http.get_json(
            f"{DERIBIT}/public/get_volatility_index_data",
            params={
                "currency": currency,
                "start_timestamp": cursor_ms,
                "end_timestamp": end_ms,
                "resolution": 3600,
            },
        )
        result = data.get("result", {})
        rows = result.get("data", [])
        if not rows:
            break

        for row in rows:
            ts_ms, _open, _high, _low, close = row[:5]
            all_rows.append((int(ts_ms), float(close)))

        continuation = result.get("continuation")
        if continuation and int(continuation) > cursor_ms:
            cursor_ms = int(continuation)
        else:
            break

        _time.sleep(0.1)

    if not all_rows:
        return pd.DataFrame({LEGACY_FEATURE_COL: pd.Series(dtype=float)})

    df = pd.DataFrame.from_records(all_rows, columns=("ts_ms", "dvol_close"))
    df["datetime"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()
    df[LEGACY_FEATURE_COL] = df["dvol_close"] / 100.0
    return pd.DataFrame(df[[LEGACY_FEATURE_COL]])


def build_training_data(
    http: HttpClient,
    lookback_hours: int = 168,
    product: str = "BTC-USD",
) -> pd.DataFrame:
    """
    Backtest helper: build training data from historical DVOL + RV.
    """
    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    start_ts = end_ts - (lookback_hours + 2) * 3600

    candles = fetch_coinbase_candles_1m(http, start_ts, end_ts, product=product)
    if len(candles) < 120:
        raise RuntimeError(
            f"Insufficient Coinbase candles for regression training: got {len(candles)}, need ≥120"
        )

    cb_df = pd.DataFrame(candles)
    cb_df["datetime"] = pd.to_datetime(cb_df["minute_end_ts"], unit="s", utc=True)
    cb_df = cb_df.set_index("datetime").sort_index()

    closes = cb_df["close"]
    log_rets = np.log(closes / closes.shift(1)).dropna()
    rv_rolling = (
        log_rets.rolling(window=60, min_periods=60).std(ddof=0) * np.sqrt(MINUTES_PER_YEAR)
    ).dropna()
    rv_hourly = rv_rolling[rv_rolling.index.minute == 0].copy()
    rv_hourly.name = RV_COL

    dvol_df = fetch_deribit_dvol_hourly(http, start_ts, end_ts)
    if dvol_df.empty:
        raise RuntimeError("No Deribit DVOL data returned for the training period")
    dvol_df.index = dvol_df.index.round("h")  # type: ignore[union-attr]
    dvol_df = dvol_df[~dvol_df.index.duplicated(keep="last")]

    merged = pd.merge(rv_hourly.to_frame(), dvol_df, left_index=True, right_index=True, how="inner")
    merged[FEATURE_COL] = [
        implied_vol_proxy(float(implied), float(realized))
        for implied, realized in zip(merged[LEGACY_FEATURE_COL], merged[RV_COL])
    ]
    merged[TARGET_COL] = merged[RV_COL].shift(-1)
    return merged.dropna()


def implied_vol_proxy(implied_current: float, rv_trailing: float) -> float:
    """
    Weighted implied/realized feature used by both backtest and live inference.

    Historical backtests use DVOL as the Deribit implied-vol proxy; live trading
    uses the near-ATM Deribit options IV.
    """
    proxy, _ = blend_vol(float(implied_current), float(rv_trailing))
    return float(proxy)


def fixed_live_iv_proxy(implied_current: float, rv_trailing: float) -> float:
    proxy, _ = fixed_live_blend(float(implied_current), float(rv_trailing))
    return float(proxy)


# ---------------------------------------------------------------------------
# Step 1 — Historical Data Pipeline & Alignment
# ---------------------------------------------------------------------------


def build_live_training_data(
    http: HttpClient,
    iv_cache_path: str,
    lookback_hours: int = 168,
    min_obs: int = 24,
    product: str = "BTC-USD",
) -> pd.DataFrame:
    """
    Build a strictly aligned live-training DataFrame from cached ATM IV snapshots.

    Columns produced
    ----------------
    IV_Proxy_Current  : fixed 75/25 implied/RV proxy at top of hour (annualized).
    RV_Trailing       : trailing 60-min realized vol at top of hour (annualized).
    Target_RV_Forward : next hour's trailing RV — the regression target.

    The forward target is constructed via ``shift(-1)`` so there is **no
    lookahead bias**: the model learns to predict the NEXT hour's RV from
    information available NOW.
    """
    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    # Extra 2 h: one for rolling-window warm-up, one for forward target
    start_ts = end_ts - (lookback_hours + 2) * 3600

    # --- Coinbase RV (The Reality) -----------------------------------
    _log.info("Fetching %d hours of Coinbase 1-min candles…", lookback_hours + 2)
    candles = fetch_coinbase_candles_1m(http, start_ts, end_ts, product=product)
    if len(candles) < 120:
        raise RuntimeError(
            f"Insufficient Coinbase candles for regression training: "
            f"got {len(candles)}, need ≥120"
        )

    cb_df = pd.DataFrame(candles)
    cb_df["datetime"] = pd.to_datetime(
        cb_df["minute_end_ts"], unit="s", utc=True,
    )
    cb_df = cb_df.set_index("datetime").sort_index()

    closes = cb_df["close"]
    log_rets = np.log(closes / closes.shift(1)).dropna()

    rv_rolling = (
        log_rets
        .rolling(window=60, min_periods=60)
        .std(ddof=0)
        * np.sqrt(MINUTES_PER_YEAR)
    ).dropna()

    # Snapshot at the top of each hour (minute_end_ts == XX:00:00)
    rv_hourly = rv_rolling[rv_rolling.index.minute == 0].copy()
    rv_hourly.name = "RV_Trailing"

    # --- Cached live Deribit ATM IV snapshots -------------------------
    records = read_live_iv_snapshots(iv_cache_path)
    iv_rows: list[tuple[int, float]] = []
    for rec in records:
        try:
            ts_s = int(rec["ts_s"])
            sigma_implied = float(rec["sigma_implied"])
        except Exception:
            continue
        if sigma_implied <= 0:
            continue
        if ts_s < start_ts or ts_s > end_ts:
            continue
        iv_rows.append((ts_s, sigma_implied))

    if not iv_rows:
        raise RuntimeError("No cached live implied-vol data available for regression training")

    iv_df = pd.DataFrame.from_records(iv_rows, columns=("ts_s", "sigma_implied"))
    iv_df["datetime"] = pd.to_datetime(iv_df["ts_s"], unit="s", utc=True)
    iv_df = iv_df.set_index("datetime").sort_index()
    iv_df[LEGACY_FEATURE_COL] = iv_df["sigma_implied"]
    iv_df.index = iv_df.index.round("h")  # type: ignore[union-attr]
    iv_df = iv_df[~iv_df.index.duplicated(keep="last")]

    # --- Alignment & Target Generation --------------------------------
    merged = pd.merge(
        rv_hourly.to_frame(),
        iv_df[[LEGACY_FEATURE_COL]],
        left_index=True,
        right_index=True,
        how="inner",
    )

    merged[FEATURE_COL] = [
        fixed_live_iv_proxy(float(implied), float(realized))
        for implied, realized in zip(merged[LEGACY_FEATURE_COL], merged[RV_COL])
    ]
    merged[TARGET_COL] = merged[RV_COL].shift(-1)
    merged = merged.dropna()
    if len(merged) < int(min_obs):
        raise RuntimeError(
            f"Insufficient cached live implied-vol observations for regression training: "
            f"got {len(merged)}, need >= {int(min_obs)}"
        )

    _log.info(
        "Training data ready: %d aligned hourly observations [%s … %s]",
        len(merged),
        str(merged.index[0]) if len(merged) else "N/A",
        str(merged.index[-1]) if len(merged) else "N/A",
    )
    return merged


# ---------------------------------------------------------------------------
# Step 2 — Volatility Regression Engine
# ---------------------------------------------------------------------------


class VolatilityRegression:
    """
    Encompassing Regression for live implied-vol / RV blending.

    Model:
        Target_RV_Forward ~ β₀ + β₁·IV_Proxy_Current + β₂·RV_Trailing

    After fitting, call ``predict(iv_proxy_current, rv_trailing)`` to obtain
    the regression-adjusted sigma for the next hour.
    """

    def __init__(self) -> None:
        self._model: Optional[LinearRegression] = None
        self.r_squared: float = 0.0
        self.coefficients: Optional[np.ndarray] = None
        self.intercept: float = 0.0
        self.n_obs: int = 0
        self.feature_name: str = FEATURE_COL

    @property
    def is_fitted(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def fit(self, df: pd.DataFrame) -> "VolatilityRegression":
        """
        Fit the OLS regression on a training DataFrame produced by
        :func:`build_training_data`.
        """
        feature_col = FEATURE_COL if FEATURE_COL in df.columns else LEGACY_FEATURE_COL
        required = {feature_col, RV_COL, TARGET_COL}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")

        if len(df) < 10:
            raise ValueError(
                f"Need ≥10 observations for a meaningful regression, got {len(df)}"
            )

        X = df[[feature_col, RV_COL]].values
        y = df[TARGET_COL].values

        model = LinearRegression()
        model.fit(X, y)

        self._model = model
        self.r_squared = float(model.score(X, y))
        self.coefficients = model.coef_.copy()
        self.intercept = float(model.intercept_)
        self.n_obs = len(df)
        self.feature_name = feature_col
        coeffs = self.coefficients
        if coeffs is None:
            raise RuntimeError("Regression coefficients missing after fit")

        _log.info(
            "Regression fit: R²=%.4f  β₀=%.6f  β_iv_proxy=%.4f  β_rv=%.4f  n=%d",
            self.r_squared,
            self.intercept,
            coeffs[0],
            coeffs[1],
            self.n_obs,
        )
        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, iv_proxy_current: float, rv_trailing: float) -> float:
        """
        Predict adjusted_sigma for the next hour from live market inputs.

        Parameters
        ----------
        iv_proxy_current : float
            Current weighted implied/RV proxy as annualized decimal.
        rv_trailing : float
            Trailing 60-min annualized realized vol (decimal).

        Returns
        -------
        adjusted_sigma : float
            Predicted annualized volatility for the next hour (decimal).
            Floored at 1e-6 to avoid non-positive sigma downstream.
        """
        if self._model is None:
            raise RuntimeError("Model not fitted — call fit() first.")

        X = np.array([[iv_proxy_current, rv_trailing]])
        pred = float(self._model.predict(X)[0])
        return max(pred, 1e-6)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def fit_from_live_cache(
        self,
        http: HttpClient,
        iv_cache_path: str,
        lookback_hours: int = 168,
        min_obs: int = 24,
        product: str = "BTC-USD",
    ) -> "VolatilityRegression":
        """Build training data from cached live ATM IV history and fit."""
        df = build_live_training_data(
            http,
            iv_cache_path=iv_cache_path,
            lookback_hours=lookback_hours,
            min_obs=min_obs,
            product=product,
        )
        return self.fit(df)

    def fit_from_api(
        self,
        http: HttpClient,
        lookback_hours: int = 168,
        product: str = "BTC-USD",
    ) -> "VolatilityRegression":
        """Backtest-compatible helper using historical DVOL + RV."""
        df = build_training_data(
            http,
            lookback_hours=lookback_hours,
            product=product,
        )
        return self.fit(df)

    def summary(self) -> str:
        """Human-readable model summary string."""
        if self.coefficients is None:
            return "VolatilityRegression(not fitted)"
        return (
            f"VolatilityRegression("
            f"R²={self.r_squared:.4f}, "
            f"β₀={self.intercept:.6f}, "
            f"β_iv_proxy={self.coefficients[0]:.4f}, "
            f"β_rv={self.coefficients[1]:.4f}, "
            f"n={self.n_obs})"
        )
