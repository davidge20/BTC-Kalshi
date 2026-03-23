"""
vol_regression.py — Encompassing Regression for optimal volatility blending.

Combines Deribit's DVOL index (forward-looking implied vol) with Coinbase's
trailing realized volatility to produce an adjusted_sigma via OLS regression.

The pipeline (no lookahead bias):
  1. Fetch historical Coinbase 1-min candles → rolling 60-min RV, hourly snapshots
  2. Fetch historical Deribit DVOL hourly snapshots
  3. Align on top-of-hour timestamps, create forward-looking target
  4. Fit OLS: Target_RV_Forward ~ β₀ + β₁·DVOL_Current + β₂·RV_Trailing
  5. At runtime, predict adjusted_sigma from live DVOL + live RV
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

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Historical data fetchers
# ---------------------------------------------------------------------------


def fetch_deribit_dvol_hourly(
    http: HttpClient,
    start_ts: int,
    end_ts: int,
    currency: str = "BTC",
) -> pd.DataFrame:
    """
    Fetch hourly Deribit DVOL index snapshots.

    Returns a DataFrame with a UTC DatetimeIndex (top-of-hour) and a single
    column ``DVOL_Current`` expressed as an annualized decimal
    (e.g. DVOL 60 → 0.60).
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

        _time.sleep(0.1)  # polite rate-limit padding

    if not all_rows:
        return pd.DataFrame(columns=["DVOL_Current"])

    df = pd.DataFrame(all_rows, columns=["ts_ms", "dvol_close"])
    df["datetime"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()
    df["DVOL_Current"] = df["dvol_close"] / 100.0
    return pd.DataFrame(df[["DVOL_Current"]])


# ---------------------------------------------------------------------------
# Step 1 — Historical Data Pipeline & Alignment
# ---------------------------------------------------------------------------


def build_training_data(
    http: HttpClient,
    lookback_hours: int = 168,
    product: str = "BTC-USD",
) -> pd.DataFrame:
    """
    Build a strictly aligned historical DataFrame for regression training.

    Columns produced
    ----------------
    DVOL_Current      : Deribit DVOL at top of hour (annualized decimal).
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

    # --- Deribit DVOL (The Expectation) -------------------------------
    _log.info("Fetching %d hours of Deribit DVOL data…", lookback_hours + 2)
    dvol_df = fetch_deribit_dvol_hourly(http, start_ts, end_ts)

    if dvol_df.empty:
        raise RuntimeError(
            "No Deribit DVOL data returned for the training period"
        )

    # Floor to top-of-hour so both series share the same key
    dvol_df.index = dvol_df.index.round("h")  # type: ignore[union-attr]
    dvol_df = dvol_df[~dvol_df.index.duplicated(keep="last")]

    # --- Alignment & Target Generation --------------------------------
    merged = pd.merge(
        rv_hourly.to_frame(),
        dvol_df,
        left_index=True,
        right_index=True,
        how="inner",
    )

    merged["Target_RV_Forward"] = merged["RV_Trailing"].shift(-1)
    merged = merged.dropna()

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
    Encompassing Regression for optimal DVOL / RV blending.

    Model:
        Target_RV_Forward ~ β₀ + β₁·DVOL_Current + β₂·RV_Trailing

    After fitting, call ``predict(dvol_current, rv_trailing)`` to obtain
    the regression-adjusted sigma for the next hour.
    """

    def __init__(self) -> None:
        self._model: Optional[LinearRegression] = None
        self.r_squared: float = 0.0
        self.coefficients: Optional[np.ndarray] = None
        self.intercept: float = 0.0
        self.n_obs: int = 0

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
        required = {"DVOL_Current", "RV_Trailing", "Target_RV_Forward"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")

        if len(df) < 10:
            raise ValueError(
                f"Need ≥10 observations for a meaningful regression, got {len(df)}"
            )

        X = df[["DVOL_Current", "RV_Trailing"]].values
        y = df["Target_RV_Forward"].values

        model = LinearRegression()
        model.fit(X, y)

        self._model = model
        self.r_squared = float(model.score(X, y))
        self.coefficients = model.coef_.copy()
        self.intercept = float(model.intercept_)
        self.n_obs = len(df)

        _log.info(
            "Regression fit: R²=%.4f  β₀=%.6f  β_dvol=%.4f  β_rv=%.4f  n=%d",
            self.r_squared,
            self.intercept,
            self.coefficients[0],
            self.coefficients[1],
            self.n_obs,
        )
        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, dvol_current: float, rv_trailing: float) -> float:
        """
        Predict adjusted_sigma for the next hour from live market inputs.

        Parameters
        ----------
        dvol_current : float
            Current Deribit DVOL as annualized decimal (DVOL 60 → 0.60).
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

        X = np.array([[dvol_current, rv_trailing]])
        pred = float(self._model.predict(X)[0])
        return max(pred, 1e-6)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def fit_from_api(
        self,
        http: HttpClient,
        lookback_hours: int = 168,
        product: str = "BTC-USD",
    ) -> "VolatilityRegression":
        """Fetch training data and fit in a single call."""
        df = build_training_data(
            http, lookback_hours=lookback_hours, product=product,
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
            f"β_dvol={self.coefficients[0]:.4f}, "
            f"β_rv={self.coefficients[1]:.4f}, "
            f"n={self.n_obs})"
        )
