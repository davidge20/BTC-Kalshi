"""
monte_carlo.py — GBM Monte Carlo simulation for binary option pricing.

Simulates many price paths under Geometric Brownian Motion with zero drift
to estimate P(S_T ≥ K).  While the analytical lognormal model gives
the exact answer under the same assumptions, the MC framework is kept
because it extends naturally to jump-diffusion or stochastic-vol models.

Usage:
    # Generate terminal prices once, reuse across all strikes
    terminals = monte_carlo_terminal_prices(spot, sigma, minutes_left)
    p1 = mc_prob_above(terminals, strike_1)
    p2 = mc_prob_above(terminals, strike_2)
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from kalshi_edge.constants import MINUTES_PER_YEAR


def monte_carlo_terminal_prices(
    spot: float,
    sigma_ann: float,
    minutes_left: float,
    n_paths: int = 10_000,
    n_steps: int = 60,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate GBM paths and return terminal prices.

    GBM step (zero drift):
        S_{t+dt} = S_t · exp((-σ²/2)·dt + σ·√dt·Z)
    """
    if minutes_left <= 0 or sigma_ann <= 0:
        return np.full(n_paths, spot)

    rng = np.random.default_rng(seed)

    dt = (minutes_left / MINUTES_PER_YEAR) / n_steps
    drift = -0.5 * sigma_ann**2 * dt
    diffusion = sigma_ann * math.sqrt(dt)

    z = rng.standard_normal((n_paths, n_steps))
    log_increments = drift + diffusion * z
    log_total = log_increments.sum(axis=1)

    return spot * np.exp(log_total)


def mc_prob_above(terminal_prices: np.ndarray, strike: float) -> float:
    """Fraction of simulated paths ending at or above *strike*."""
    if terminal_prices.size == 0:
        return 0.0
    return float(np.mean(terminal_prices >= strike))


def run_monte_carlo_simulation(
    spot: float,
    strike: float,
    sigma: float,
    minutes_left: float = 60.0,
    n_paths: int = 10_000,
    n_steps: int = 60,
    seed: Optional[int] = None,
) -> float:
    """
    High-level GBM Monte Carlo: returns P(S_T ≥ strike) directly.

    Designed for 1-hour Kalshi prediction markets.  Uses ``adjusted_sigma``
    from the encompassing regression as the volatility input.

    GBM step (zero drift):
        S_{t+Δt} = S_t · exp((-σ²/2)·Δt + σ·√Δt·Z)

    With minutes_left=60 and n_steps=60, each step is Δt = 1/525600
    (one minute in years), giving a minute-by-minute simulation.
    """
    terminals = monte_carlo_terminal_prices(
        spot=spot,
        sigma_ann=sigma,
        minutes_left=minutes_left,
        n_paths=n_paths,
        n_steps=n_steps,
        seed=seed,
    )
    return mc_prob_above(terminals, strike)
