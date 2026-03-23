"""
pipeline.py — high-level orchestration called by run.py.

Coordinates: fetch event -> extract ladder -> compute MarketState -> evaluate ladder.

Keeping this glue in one place prevents circular imports between data-fetching,
model, and evaluation modules.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional

from kalshi_edge.http_client import HttpClient
from kalshi_edge.data.kalshi.client import get_event
from kalshi_edge.data.kalshi.models import above_markets_from_event, event_ticker_from_url
from kalshi_edge.market_state import build_market_state, MarketState
from kalshi_edge.ladder_eval import evaluate_ladder, LadderRow


@dataclass
class EvaluationResult:
    event_ticker: str
    event_title: str
    minutes_left: float
    market_state: MarketState
    rows: List[LadderRow]


def evaluate_event(
    *,
    event: Optional[str] = None,
    url: Optional[str] = None,
    max_strikes: int = 120,
    band_pct: float = 25.0,
    sort: str = "ev",
    fee_cents: int = 1,
    depth_window_cents: int = 2,
    threads: int = 10,
    iv_band_pct: float = 3.0,
    mc_paths: int = 10_000,
    mc_steps: int = 60,
    debug_http: bool = False,
    vol_model: object | None = None,
) -> EvaluationResult:
    """
    Evaluate a single Kalshi event.

    Supply either ``event="KXBTCD-..."`` or ``url="https://kalshi.com/..."``.

    Volatility priority:
      1. Encompassing Regression (vol_model.predict)  ← new primary
      2. GARCH(1,1) on Coinbase 1-min returns
      3. Heuristic IV/RV blend                        (fallback)

    Parameters
    ----------
    vol_model : VolatilityRegression | None
        A fitted regression model.  When provided, ``adjusted_sigma`` from
        the DVOL/RV regression becomes the primary volatility for both
        the analytical and Monte Carlo models.
    """
    if not event and not url:
        raise ValueError("Must provide event or url.")

    event_ticker = event.upper() if event else event_ticker_from_url(url or "")
    http = HttpClient(debug=debug_http)

    event_json = get_event(http, event_ticker)
    event_title, above_markets, minutes_left = above_markets_from_event(event_json)
    if not above_markets:
        raise RuntimeError("Event had no ABOVE ladder markets (-T). Confirm you used the ABOVE/BELOW event.")

    ms = build_market_state(
        http=http,
        minutes_left=minutes_left,
        iv_band_pct=iv_band_pct,
        vol_model=vol_model,
    )

    rows = evaluate_ladder(
        http=http,
        markets=above_markets,
        spot=ms.spot,
        sigma_blend=ms.sigma_blend,
        minutes_left=minutes_left,
        max_strikes=max_strikes,
        band_pct=band_pct,
        fee_cents=fee_cents,
        depth_window_cents=depth_window_cents,
        sort_mode=sort,
        threads=threads,
        mc_paths=mc_paths,
        mc_steps=mc_steps,
    )

    return EvaluationResult(
        event_ticker=event_ticker,
        event_title=event_title,
        minutes_left=minutes_left,
        market_state=ms,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Model comparison helper
# ---------------------------------------------------------------------------


def compare_pricing_models(
    spot: float,
    strike: float,
    minutes_left: float,
    dvol_current: float,
    rv_trailing: float,
    sigma_adjusted: float,
    mc_paths: int = 10_000,
    mc_steps: int = 60,
) -> Dict[str, float]:
    """
    Run both the analytical stochastic model and Monte Carlo simulation
    for a single strike, returning a clean comparison dictionary.

    Both models receive the same ``adjusted_sigma`` produced by the
    encompassing regression so results are directly comparable.
    """
    from kalshi_edge.math_models import lognormal_prob_above
    from kalshi_edge.monte_carlo import run_monte_carlo_simulation

    stochastic_prob = lognormal_prob_above(
        spot, strike, sigma_adjusted, minutes_left,
    )
    monte_carlo_prob = run_monte_carlo_simulation(
        spot, strike, sigma_adjusted, minutes_left, mc_paths, mc_steps,
    )

    return {
        "spot": spot,
        "strike": strike,
        "minutes_left": minutes_left,
        "DVOL_Current": dvol_current,
        "RV_Trailing": rv_trailing,
        "adjusted_sigma": sigma_adjusted,
        "stochastic_prob": stochastic_prob,
        "monte_carlo_prob": monte_carlo_prob,
    }
