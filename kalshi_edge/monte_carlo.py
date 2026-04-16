"""
monte_carlo.py — Student's t-distribution Monte Carlo simulation.

Simulates price paths using a Student's t-distribution to better capture 
Bitcoin's fat tails compared to standard Geometric Brownian Motion (lognormal).

Usage:
    # Convert DVOL (annualized) to hourly vol
    hourly_vol = dvol_annualized / math.sqrt(365 * 24)
    
    # Calculate probability
    prob = t_dist_prob_above(
        current_price=70781,
        strike_price=71000,
        hourly_vol=hourly_vol,
        time_remaining_hours=2.5
    )
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy.stats import t

def simulate_t_dist_terminal_prices(
    current_price: float,
    hourly_vol: float,
    time_remaining_hours: float,
    df: float = 3.0,
    n_paths: int = 100_000,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Simulate terminal prices using a Student's t-distribution.
    
    Args:
        current_price: Current price of the asset.
        hourly_vol: Volatility per hour (e.g., 0.012 for 1.2%).
        time_remaining_hours: Time remaining until expiry in hours.
        df: Degrees of freedom for the t-distribution (default 3 for fat tails).
        n_paths: Number of Monte Carlo simulations to run.
        seed: Optional random seed for reproducibility.
        
    Returns:
        Array of simulated terminal prices.
    """
    if time_remaining_hours <= 0 or hourly_vol <= 0:
        return np.full(n_paths, current_price)

    rng = np.random.default_rng(seed)

    # Scale the hourly volatility to the remaining time 'T'
    sigma_t = hourly_vol * math.sqrt(time_remaining_hours)

    # Generate random returns from a standard t-distribution
    # We use scipy.stats.t.rvs or numpy's standard_t. Numpy is generally faster for large arrays.
    standard_t_returns = rng.standard_t(df, size=n_paths)
    
    # Scale returns by our time-adjusted volatility (assuming drift = 0)
    simulated_returns = sigma_t * standard_t_returns

    # Calculate final prices: Price_final = current_price * exp(simulated_return)
    terminal_prices = current_price * np.exp(simulated_returns)

    return terminal_prices


def t_dist_prob_above(
    current_price: float,
    strike_price: float,
    hourly_vol: float,
    time_remaining_hours: float,
    df: float = 3.0,
    n_paths: int = 100_000,
    seed: Optional[int] = None,
) -> float:
    """
    Calculate the probability of the price ending > strike_price using a t-distribution.
    """
    terminal_prices = simulate_t_dist_terminal_prices(
        current_price=current_price,
        hourly_vol=hourly_vol,
        time_remaining_hours=time_remaining_hours,
        df=df,
        n_paths=n_paths,
        seed=seed,
    )
    
    # Output: The probability (0.0 to 1.0) of the price ending > strike_price
    return float(np.mean(terminal_prices > strike_price))


def convert_annualized_vol_to_hourly(sigma_ann: float) -> float:
    """
    Helper to convert annualized volatility (like Deribit DVOL) to hourly volatility.
    
    Args:
        sigma_ann: Annualized volatility as a decimal (e.g., 0.60 for 60% DVOL).
        
    Returns:
        Hourly volatility.
    """
    # 365 days * 24 hours = 8760 hours in a year
    return sigma_ann / math.sqrt(365 * 24)
