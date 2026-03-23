"""
garch.py — GARCH(1,1) volatility forecasting from 1-minute log returns.

Replaces the heuristic IV/RV blend with a statistically grounded
conditional variance forecast.  The workflow:

  1. Receive 1-minute log returns (from Coinbase).
  2. Scale returns ×100 (percentage form) so the ARCH optimizer converges
     reliably on the small decimal values typical of 1-min crypto returns.
  3. Fit a GARCH(1,1) with constant mean.
  4. Forecast conditional variance for each of the next `horizon` minutes.
  5. Sum the per-minute variances → 1-hour variance.
  6. Reverse the ×100 scaling, sqrt → hourly σ, then annualize.

The result is an annualized volatility suitable for plugging directly
into both the analytical (lognormal) model and the GBM Monte Carlo.
"""

from __future__ import annotations

import math
import logging
import warnings
from typing import Tuple

import numpy as np
import pandas as pd
from arch import arch_model
from arch.univariate.base import DataScaleWarning

HOURS_PER_YEAR: float = 365.0 * 24.0

log = logging.getLogger(__name__)


def forecast_garch_volatility(
    returns: pd.Series,
    horizon: int = 60,
) -> Tuple[float, str]:
    """
    Fit GARCH(1,1) on 1-minute log returns and forecast the next hour's vol.

    Parameters
    ----------
    returns : pd.Series
        1-minute log returns as decimals (e.g. 0.0003 for a 0.03% move).
    horizon : int
        Number of 1-minute steps to forecast (60 = 1 hour).

    Returns
    -------
    (sigma_ann, note) where sigma_ann is annualized volatility as a decimal
    and note is a human-readable diagnostic string.
    """
    if len(returns) < 60:
        raise ValueError(f"Need ≥60 returns for GARCH fit, got {len(returns)}")

    scaled = returns * 100.0

    model = arch_model(scaled, vol="Garch", p=1, q=1, mean="Constant", rescale=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DataScaleWarning)
        result = model.fit(disp="off", show_warning=False)

    fc = result.forecast(horizon=horizon)
    variance_sum = float(fc.variance.iloc[-1].sum())

    hourly_variance = variance_sum / 10_000.0
    hourly_std = math.sqrt(max(hourly_variance, 0.0))
    sigma_ann = hourly_std * math.sqrt(HOURS_PER_YEAR)

    omega = result.params.get("omega", 0.0)
    alpha = result.params.get("alpha[1]", 0.0)
    beta = result.params.get("beta[1]", 0.0)
    persistence = alpha + beta

    note = (
        f"garch(1,1) ω={omega:.6f} α={alpha:.4f} β={beta:.4f} "
        f"persistence={persistence:.4f} horizon={horizon}min "
        f"n_obs={len(returns)}"
    )
    return sigma_ann, note
