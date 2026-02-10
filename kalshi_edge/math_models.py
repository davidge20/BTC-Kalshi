"""
math_models.py

Probability model for "BTC >= strike at close".

We use a *very* simple model:
- BTC follows a lognormal distribution over remaining time
- drift is assumed to be 0 for short horizons
- volatility is the blended sigma we estimate in MarketState
"""

from __future__ import annotations
import math
from kalshi_edge.constants import MINUTES_PER_YEAR


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def norm_cdf(x: float) -> float:
    """Standard normal CDF using erf, avoiding scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def lognormal_prob_above(spot: float, strike: float, sigma_ann: float, minutes_left: float) -> float:
    """
    P(S_T >= K) under lognormal with zero drift.

    Model assumption:
      ln(S_T / S_0) ~ Normal(0, sigma^2 * t)

    Where:
      sigma is annualized volatility (decimal, e.g. 1.50 = 150%)
      t is in years
    """
    if strike <= 0 or spot <= 0:
        return 0.0

    if minutes_left <= 0:
        return 1.0 if spot >= strike else 0.0

    t_years = minutes_left / MINUTES_PER_YEAR
    vol_sqrt_t = sigma_ann * math.sqrt(t_years)
    if vol_sqrt_t <= 0:
        return 1.0 if spot >= strike else 0.0

    z = math.log(strike / spot) / vol_sqrt_t
    return clamp01(1.0 - norm_cdf(z))


def expected_one_sigma_move_pct(sigma_ann: float, minutes_left: float) -> float:
    """
    Expected 1-sigma move as a percent over the remaining time.

    If sigma_ann is annualized vol, then sigma over horizon t is:
      sigma_t = sigma_ann * sqrt(t)
    """
    t_years = minutes_left / MINUTES_PER_YEAR
    return sigma_ann * math.sqrt(t_years) * 100.0