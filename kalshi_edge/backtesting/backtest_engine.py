"""
Minute-cadence backtest engine for Kalshi BTC ladder events.

Volatility hierarchy (matches live trader):
  1. Regression on weighted implied/RV proxy + RV   (primary)
  2. GARCH(1,1) on trailing Coinbase 1-min returns
  3. Trailing realized vol fallback
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import statistics
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.backtesting.cache import FileCache
from kalshi_edge.backtesting.coinbase_history import build_close_by_minute_ts, fetch_coinbase_candles_1m
from kalshi_edge.backtesting.kalshi_candles import (
    fetch_batch_market_candles_1m,
    fetch_market_candles_1m,
    get_historical_cutoff,
    list_events,
    list_markets_for_event,
)
from kalshi_edge.constants import MINUTES_PER_YEAR
from kalshi_edge.data.kalshi.models import market_strike_from_floor
from kalshi_edge.exit_rules import ExitMarketSnapshot, evaluate_exit, should_pause_new_entries
from kalshi_edge.math_models import clamp01
from kalshi_edge.monte_carlo import simulate_t_dist_terminal_prices, convert_annualized_vol_to_hourly
from kalshi_edge.strategy_config import BacktestConfig, StrategyConfig, config_to_dict
from kalshi_edge.util.time import parse_iso8601

_log = logging.getLogger(__name__)


def dollars_to_cents(raw: Any) -> Optional[int]:
    """
    Convert either cents-like or dollar/probability-like inputs to cents.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return int(raw) if 0 <= int(raw) <= 100 else None
    if isinstance(raw, float):
        v = float(raw)
    elif isinstance(raw, str):
        s = raw.strip().replace("$", "").replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
        except Exception:
            return None
    else:
        return None
    cents = int(round(v * 100.0)) if v <= 1.0 else int(round(v))
    return cents if 0 <= cents <= 100 else None


def derive_no_quotes(yes_bid_cents: Optional[int], yes_ask_cents: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    """
    Derive NO bid/ask from YES quotes:
      nbid = 100 - yask
      nask = 100 - ybid
    """
    nbid = (100 - int(yes_ask_cents)) if yes_ask_cents is not None else None
    nask = (100 - int(yes_bid_cents)) if yes_bid_cents is not None else None
    return nbid, nask


def annualized_realized_vol_from_closes(closes: List[float]) -> float:
    if len(closes) < 2:
        return 0.0
    rets: List[float] = []
    for i in range(1, len(closes)):
        a, b = float(closes[i - 1]), float(closes[i])
        if a <= 0 or b <= 0:
            continue
        rets.append(math.log(b / a))
    if len(rets) < 1:
        return 0.0
    if len(rets) == 1:
        return float(abs(rets[0]) * math.sqrt(MINUTES_PER_YEAR))
    stdev = statistics.pstdev(rets)
    return float(stdev * math.sqrt(MINUTES_PER_YEAR))


def rolling_annualized_realized_vol(closes: List[float], window: int) -> List[float]:
    out: List[float] = []
    w = max(2, int(window))
    for i in range(len(closes)):
        chunk = closes[max(0, i - w + 1) : i + 1]
        out.append(annualized_realized_vol_from_closes(chunk))
    return out


def max_acceptable_price_cents(*, p_win: float, min_ev: float, fee_buffer_cents: int) -> int:
    x = int(math.floor(100.0 * (float(p_win) - float(min_ev)))) - int(fee_buffer_cents)
    return max(0, min(99, x))


def edge_at_price(*, p_win: float, price_cents: int, fee_cents: int) -> float:
    return float(p_win) - (float(price_cents + fee_cents) / 100.0)


def kelly_fraction_binary(*, p_win: float, total_cost_dollars: float) -> float:
    cost = float(total_cost_dollars)
    if cost <= 0.0 or cost >= 1.0:
        return 0.0
    p = clamp01(float(p_win))
    return max(0.0, float((p - cost) / (1.0 - cost)))


def desired_total_contracts(
    *,
    sizing_mode: str,
    order_size: int,
    max_contracts_per_market: int,
    price_cents: int,
    fee_cents: int,
    p_win: float,
    bankroll_dollars: float,
    kelly_fraction_scale: float,
    current_contracts: int,
) -> int:
    if str(sizing_mode) != "kelly":
        return min(int(max_contracts_per_market), int(current_contracts) + int(order_size))

    total_cost_dollars = float(price_cents + fee_cents) / 100.0
    if total_cost_dollars <= 0.0 or bankroll_dollars <= 0.0:
        return int(current_contracts)
    kelly_f = float(kelly_fraction_binary(p_win=float(p_win), total_cost_dollars=total_cost_dollars))
    target_dollars = float(bankroll_dollars) * float(kelly_fraction_scale) * float(kelly_f)
    target_contracts = int(math.floor(target_dollars / total_cost_dollars))
    return min(int(max_contracts_per_market), max(0, int(target_contracts)))


def _candle_at_or_before(
    cmap: Dict[int, Candle],
    keys: List[int],
    ts: int,
) -> Optional[Candle]:
    idx = bisect.bisect_right(keys, int(ts)) - 1
    if idx < 0:
        return None
    return cmap.get(keys[idx])


@dataclass
class Candle:
    ts: int
    yes_bid_cents: Optional[int]
    yes_ask_cents: Optional[int]


@dataclass
class MarketMeta:
    ticker: str
    event_ticker: str
    strike: float
    subtitle: str
    close_ts: int
    open_ts: int
    result: Optional[str] = None
    settlement_value: Optional[int] = None


@dataclass
class Fill:
    ts: int
    event_ticker: str
    market_ticker: str
    side: str
    contracts: int
    entry_price_cents: int
    fee_cents: int
    p_yes: float
    p_win: float
    ev: float
    spread: Optional[int]
    sigma: float = 0.0
    vol_source: str = ""


@dataclass
class Position:
    event_ticker: str
    market_ticker: str
    side: str
    total_count: int = 0
    total_cost_dollars: float = 0.0
    total_fee_dollars: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    last_fill_ts: Optional[int] = None


@dataclass
class ClosedTrade:
    event_ticker: str
    market_ticker: str
    side: str
    contracts: int
    exit_ts: int
    exit_reason: str
    entry_cost_dollars: float
    exit_net_dollars: float
    pnl_dollars: float


@dataclass
class EventResult:
    event_ticker: str
    close_ts: int
    trades: int
    contracts: int
    pnl: float
    win_rate: float


@dataclass
class BacktestSummary:
    series_ticker: str
    start_ts: int
    end_ts: int
    events_scanned: int
    events_simulated: int
    trades: int
    contracts: int
    total_pnl: float
    win_rate: float
    per_event: List[EventResult]
    log_path: str


@dataclass
class _Candidate:
    event_ticker: str
    market_ticker: str
    strike: float
    side: str  # yes|no
    price_cents: int
    fee_cents: int
    p_yes: float
    edge_pp: float
    spread: Optional[int]
    ts: int


def _parse_csv(s: Optional[str]) -> List[str]:
    if not isinstance(s, str):
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _event_ticker(e: Dict[str, Any]) -> Optional[str]:
    t = e.get("ticker") or e.get("event_ticker")
    return str(t) if isinstance(t, str) and t else None


def _event_close_ts(e: Dict[str, Any]) -> Optional[int]:
    v = (
        e.get("close_time")
        or e.get("closeTime")
        or e.get("strike_date")
        or e.get("strikeDate")
        or e.get("expiration_time")
    )
    if not isinstance(v, str):
        return None
    try:
        return int(parse_iso8601(v).timestamp())
    except Exception:
        return None


def _market_meta_from_row(row: Dict[str, Any], *, event_ticker: str) -> Optional[MarketMeta]:
    t = row.get("ticker") or row.get("market_ticker")
    if not isinstance(t, str):
        return None
    strike = market_strike_from_floor(row)
    if strike is None:
        return None
    close_s = row.get("close_time") or row.get("closeTime")
    if not isinstance(close_s, str):
        return None
    try:
        close_ts = int(parse_iso8601(close_s).timestamp())
    except Exception:
        return None
        
    open_s = row.get("open_time") or row.get("openTime")
    try:
        open_ts = int(parse_iso8601(open_s).timestamp()) if isinstance(open_s, str) else int(close_ts)
    except Exception:
        open_ts = int(close_ts)

    subtitle = str(row.get("subtitle") or row.get("title") or "")
    result = row.get("result")
    settlement_value = row.get("settlement_value")
    settlement_value_i = int(settlement_value) if isinstance(settlement_value, int) else None
    return MarketMeta(
        ticker=str(t),
        event_ticker=str(event_ticker),
        strike=float(strike),
        subtitle=subtitle,
        close_ts=int(close_ts),
        open_ts=int(open_ts),
        result=str(result).lower() if isinstance(result, str) else None,
        settlement_value=settlement_value_i,
    )


def _pick_markets(markets: List[MarketMeta], spot: float, max_strikes: int, band_pct: float) -> List[MarketMeta]:
    srt = sorted(markets, key=lambda m: abs(float(m.strike) - float(spot)))
    if int(max_strikes) <= 10:
        return srt[: int(max_strikes)]
    lo = float(spot) * (1.0 - float(band_pct) / 100.0)
    hi = float(spot) * (1.0 + float(band_pct) / 100.0)
    in_band = [m for m in srt if lo <= float(m.strike) <= hi]
    if len(in_band) >= max(10, int(max_strikes) // 3):
        return in_band[: int(max_strikes)]
    return srt[: int(max_strikes)]


def _quote_spread(ybid: Optional[int], yask: Optional[int]) -> Optional[int]:
    if ybid is None or yask is None:
        return None
    return int(yask) - int(ybid)


def _valid_taker_price_cents(px: Optional[int]) -> bool:
    if px is None:
        return False
    # Kalshi binary tick is cents; do not allow synthetic free fills.
    return 1 <= int(px) <= 99


def _payout_cents_per_contract(result: Optional[str], side: str, settlement_value: Optional[int], entry_price_cents: int) -> Optional[int]:
    r = str(result or "").lower()
    s = str(side).lower()
    if r in {"yes", "no"}:
        return 100 if r == s else 0
    if r == "scalar" and settlement_value is not None:
        return int(settlement_value)
    if r == "void":
        # Return principal only; fees remain paid.
        return int(entry_price_cents)
    return None


def _ensure_log_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _append_jsonl(path: str, record: Dict[str, Any]) -> None:
    _ensure_log_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _position_avg_principal_cents(pos: Position) -> int:
    if int(pos.total_count) <= 0:
        return 0
    principal = max(0.0, float(pos.total_cost_dollars) - float(pos.total_fee_dollars))
    return int(round((principal * 100.0) / float(pos.total_count)))


def _close_position(
    *,
    positions: Dict[str, Position],
    market_cost: Dict[str, float],
    event_cost: Dict[str, float],
    event_positions: Dict[str, int],
    pos: Position,
    exit_ts: int,
    exit_reason: str,
    exit_price_cents: int,
    exit_fee_cents: int,
) -> ClosedTrade:
    contracts = int(pos.total_count)
    entry_cost = float(pos.total_cost_dollars)
    exit_net = float(contracts) * ((float(exit_price_cents) - float(exit_fee_cents)) / 100.0)
    pnl = float(exit_net - entry_cost)

    positions.pop(pos.market_ticker, None)
    market_cost.pop(pos.market_ticker, None)
    evt = str(pos.event_ticker)
    event_cost[evt] = float(max(0.0, event_cost.get(evt, 0.0) - entry_cost))
    if float(event_cost.get(evt, 0.0)) <= 0.0:
        event_cost.pop(evt, None)
    event_positions[evt] = int(max(0, int(event_positions.get(evt, 0)) - 1))
    if int(event_positions.get(evt, 0)) <= 0:
        event_positions.pop(evt, None)

    return ClosedTrade(
        event_ticker=str(pos.event_ticker),
        market_ticker=str(pos.market_ticker),
        side=str(pos.side),
        contracts=int(contracts),
        exit_ts=int(exit_ts),
        exit_reason=str(exit_reason),
        entry_cost_dollars=float(entry_cost),
        exit_net_dollars=float(exit_net),
        pnl_dollars=float(pnl),
    )


def run_backtest(
    *,
    http: Any,
    cfg: StrategyConfig,
    bt: BacktestConfig,
    start_dt: datetime,
    end_dt: datetime,
    log_path: str,
) -> BacktestSummary:
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    cache = FileCache(bt.CACHE_DIR)

    cutoff_dt = get_historical_cutoff(http)
    cutoff_ts = int(cutoff_dt.timestamp())

    events_cfg = _parse_csv(bt.EVENTS)
    if events_cfg:
        events_raw = [{"ticker": t} for t in events_cfg]
    else:
        events_raw = list_events(
            http=http,
            series_ticker=bt.SERIES_TICKER,
            start_ts=start_ts,
            end_ts=end_ts,
            status="settled",
        )
    if int(bt.MAX_EVENTS) > 0:
        if events_cfg:
            events_raw = events_raw[: int(bt.MAX_EVENTS)]
        else:
            events_raw = events_raw[-int(bt.MAX_EVENTS) :]

    # Extend Coinbase fetch backwards to cover regression + GARCH warm-up.
    _REGRESSION_LOOKBACK_HOURS = 168
    _coinbase_warmup_s = (_REGRESSION_LOOKBACK_HOURS + 2) * 3600
    coin_start_ts = start_ts - _coinbase_warmup_s
    coinbase_cache_path = cache.coinbase_candles_path("BTC-USD", coin_start_ts, end_ts)
    coin_rows = cache.read_json_gz(coinbase_cache_path)
    if not isinstance(coin_rows, list):
        coin_rows = fetch_coinbase_candles_1m(http=http, start_ts=coin_start_ts, end_ts=end_ts, product="BTC-USD")
        cache.write_json_gz(coinbase_cache_path, coin_rows)
    close_by_ts = build_close_by_minute_ts(coin_rows)
    close_keys = sorted(close_by_ts.keys())
    close_vals = [float(close_by_ts[k]) for k in close_keys]
    rv_window_closes = max(2, int(bt.REALIZED_VOL_WINDOW_MINUTES) + 1)

    # -- Log returns for GARCH (built once, reused across all events) --------
    ret_keys: List[int] = []
    ret_vals: List[float] = []
    for i in range(1, len(close_vals)):
        a, b = close_vals[i - 1], close_vals[i]
        if a > 0 and b > 0:
            ret_keys.append(close_keys[i])
            ret_vals.append(math.log(b / a))

    # -- Lazy heavy imports (keep test collection fast) ----------------------
    import pandas as pd
    from kalshi_edge.garch import forecast_garch_volatility
    from kalshi_edge.vol_regression import (
        FEATURE_COL,
        VolatilityRegression,
        fetch_deribit_dvol_hourly,
        implied_vol_proxy,
    )

    # -- Historical DVOL (fetch once, cache on disk) -------------------------
    dvol_warm_up_s = (_REGRESSION_LOOKBACK_HOURS + 2) * 3600
    dvol_start_ts = start_ts - dvol_warm_up_s
    dvol_by_hour: Dict[int, float] = {}

    dvol_cache_path = cache.dvol_hourly_path("BTC", dvol_start_ts, end_ts)
    dvol_records = cache.read_json_gz(dvol_cache_path)
    if not isinstance(dvol_records, list):
        print("[backtest] fetching historical Deribit DVOL …")
        try:
            dvol_df = fetch_deribit_dvol_hourly(http, dvol_start_ts, end_ts)
            dvol_records = [
                {"ts_s": int(idx.timestamp()), "dvol": float(row["DVOL_Current"])}
                for idx, row in dvol_df.iterrows()
            ]
            cache.write_json_gz(dvol_cache_path, dvol_records)
            print(f"[backtest] cached {len(dvol_records)} DVOL hourly snapshots")
        except Exception as e:
            print(f"[backtest] warning: DVOL fetch failed ({e}); regression will be disabled")
            dvol_records = []

    for rec in dvol_records:
        hour_ts = (int(rec["ts_s"]) // 3600) * 3600
        dvol_by_hour[hour_ts] = float(rec["dvol"])

    # -- Hourly RV snapshots (for regression training data) ------------------
    rv_by_hour: Dict[int, float] = {}
    if close_keys:
        first_hour = (close_keys[0] // 3600) * 3600
        last_hour = (close_keys[-1] // 3600) * 3600
        for hour_ts in range(first_hour, last_hour + 3600, 3600):
            idx = bisect.bisect_right(close_keys, hour_ts) - 1
            if idx < 1:
                continue
            lo = max(0, idx - (rv_window_closes - 1))
            chunk = close_vals[lo: idx + 1]
            if len(chunk) >= 2:
                rv_by_hour[hour_ts] = annualized_realized_vol_from_closes(chunk)

    # -- Volatility function caches ------------------------------------------
    _CACHE_INTERVAL = 3600
    _garch_cache: Dict[int, Optional[float]] = {}
    _regression_cache: Dict[int, Optional[float]] = {}
    garch_hits = 0
    garch_misses = 0

    def spot_at_or_before(ts: int) -> Optional[float]:
        idx = bisect.bisect_right(close_keys, int(ts)) - 1
        if idx < 0:
            return None
        return float(close_vals[idx])

    def realized_vol_at(ts: int, window_closes: Optional[int] = None) -> Optional[float]:
        idx = bisect.bisect_right(close_keys, int(ts)) - 1
        if idx < 0:
            return None
        w = int(window_closes) if window_closes is not None else int(rv_window_closes)
        lo = max(0, idx - max(2, w) + 1)
        closes = close_vals[lo : idx + 1]
        if len(closes) < 2:
            return None
        return annualized_realized_vol_from_closes(closes)

    def garch_sigma_at(ts: int) -> Optional[float]:
        nonlocal garch_hits, garch_misses
        bucket = (int(ts) // _CACHE_INTERVAL) * _CACHE_INTERVAL
        cached = _garch_cache.get(bucket)
        if cached is not None:
            garch_hits += 1
            return cached if cached > 0 else None
        if bucket in _garch_cache:
            garch_hits += 1
            return None
        garch_misses += 1
        idx = bisect.bisect_right(ret_keys, int(ts))
        if idx < 60:
            _garch_cache[bucket] = 0.0
            return None
        lo = max(0, idx - 300)
        trailing = pd.Series(ret_vals[lo:idx])
        try:
            sigma, _ = forecast_garch_volatility(trailing)
            _garch_cache[bucket] = float(sigma)
            return float(sigma)
        except Exception as e:
            _log.debug("GARCH fit failed at ts=%d: %s", ts, e)
            _garch_cache[bucket] = 0.0
            return None

    def regression_sigma_at(ts: int) -> Optional[float]:
        """
        Fit the live-style regression on trailing hourly data and predict.
        Cached per hour. Uses only data strictly before the current hour (no lookahead).
        """
        bucket = (int(ts) // _CACHE_INTERVAL) * _CACHE_INTERVAL
        if bucket in _regression_cache:
            cached = _regression_cache[bucket]
            return cached if cached is not None and cached > 0 else None

        implied_now = dvol_by_hour.get(bucket)
        rv_now = rv_by_hour.get(bucket)
        if implied_now is None or implied_now <= 0 or rv_now is None or rv_now <= 0:
            _regression_cache[bucket] = None
            return None
        feature_now = implied_vol_proxy(implied_now, rv_now)

        # Build training rows from trailing hours (target = NEXT hour's RV)
        train_feature, train_rv, train_target = [], [], []
        for h in range(1, _REGRESSION_LOOKBACK_HOURS + 1):
            h_ts = bucket - h * 3600
            h_implied = dvol_by_hour.get(h_ts)
            h_rv = rv_by_hour.get(h_ts)
            h_target = rv_by_hour.get(h_ts + 3600)
            if h_implied is not None and h_rv is not None and h_target is not None:
                train_feature.append(implied_vol_proxy(h_implied, h_rv))
                train_rv.append(h_rv)
                train_target.append(h_target)

        if len(train_feature) < 24:
            _regression_cache[bucket] = None
            return None

        try:
            df = pd.DataFrame({
                FEATURE_COL: train_feature,
                "RV_Trailing": train_rv,
                "Target_RV_Forward": train_target,
            })
            model = VolatilityRegression()
            model.fit(df)
            sigma_adj = model.predict(feature_now, rv_now)
            _regression_cache[bucket] = float(sigma_adj)
            return float(sigma_adj)
        except Exception as e:
            _log.debug("Regression fit/predict failed at ts=%d: %s", ts, e)
            _regression_cache[bucket] = None
            return None

    _use_regression = True

    _last_vol_source = None

    def volatility_at(ts: int) -> Tuple[Optional[float], str]:
        nonlocal _last_vol_source
        """Full live hierarchy: regression > GARCH > weighted implied/RV fallback."""
        val, source = None, "none"
        
        if _use_regression:
            reg = regression_sigma_at(ts)
            if reg is not None and reg > 0:
                val, source = reg, "regression"
                
        if val is None:
            g = garch_sigma_at(ts)
            if g is not None and g > 0:
                val, source = g, "garch"
            else:
                implied_now = dvol_by_hour.get((int(ts) // _CACHE_INTERVAL) * _CACHE_INTERVAL)
                rv = realized_vol_at(ts)
                if implied_now is not None and implied_now > 0 and rv is not None and rv > 0:
                    val, source = implied_vol_proxy(implied_now, rv), "weighted_proxy"
                elif rv is not None and rv > 0:
                    val, source = rv, "rv_fallback"

        if source != _last_vol_source:
            dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            if source != "regression" and source != "none":
                print(f"[backtest] ⚠️  WARNING: Volatility model fell back to {source} at {dt_str}")
            elif source == "regression" and _last_vol_source is not None:
                print(f"[backtest] ✅ Volatility model recovered to regression at {dt_str}")
            _last_vol_source = source

        return val, source

    # -- Tracking -----------------------------------------------------------
    positions: Dict[str, Position] = {}
    market_cost: Dict[str, float] = {}
    event_cost: Dict[str, float] = {}
    event_positions: Dict[str, int] = {}
    all_fills: List[Fill] = []
    closed_trades: List[ClosedTrade] = []
    per_event_results: List[EventResult] = []
    total_wins = 0
    total_settled = 0
    events_scanned = 0
    events_simulated = 0
    vol_source_counts: Dict[str, int] = {}
    realized_pnl_total = 0.0

    for ev in events_raw:
        et = _event_ticker(ev)
        if not et:
            continue
        events_scanned += 1

        mpath = cache.kalshi_markets_path(et)
        market_rows = cache.read_json_gz(mpath)
        if not isinstance(market_rows, list):
            market_rows = list_markets_for_event(http=http, event_ticker=et, cutoff_dt=cutoff_dt)
            cache.write_json_gz(mpath, market_rows)

        metas = [m for m in (_market_meta_from_row(r, event_ticker=et) for r in market_rows) if m is not None]
        if not metas:
            continue

        close_ts = _event_close_ts(ev) or min(m.close_ts for m in metas)
        if close_ts <= start_ts or close_ts > end_ts:
            continue

        if bt.ONLY_LAST_N_MINUTES is not None:
            event_start_ts = max(start_ts, close_ts - int(bt.ONLY_LAST_N_MINUTES) * 60)
        else:
            event_start_ts = max(start_ts, close_ts - 24 * 60 * 60)
        if event_start_ts >= close_ts:
            continue

        # Pre-filter: only fetch candles for strikes near spot at event
        # start.  Use MAX_STRIKES directly (don't multiply by 2) since 
        # strikes are $100 apart and massive drifts are rare.
        prefilt_spot = spot_at_or_before(event_start_ts)
        if prefilt_spot is not None:
            prefilt_n = int(bt.MAX_STRIKES)
            metas = _pick_markets(metas, spot=prefilt_spot, max_strikes=prefilt_n, band_pct=float(bt.BAND_PCT))
        if not metas:
            continue
        print(f"[backtest] event {et}: {len(metas)} strikes in pre-filter", flush=True)

        candles_by_market: Dict[str, Dict[int, Candle]] = {}
        candle_keys_by_market: Dict[str, List[int]] = {}
        live_tickers = [m.ticker for m in metas if m.close_ts >= cutoff_ts]
        batched = fetch_batch_market_candles_1m(http, live_tickers, event_start_ts, close_ts)

        for m in metas:
            cpath = cache.kalshi_candles_path(m.ticker, event_start_ts, close_ts)
            rows = cache.read_json_gz(cpath)
            if not isinstance(rows, list):
                rows = batched.get(m.ticker)
                if rows is None:
                    try:
                        rows = fetch_market_candles_1m(
                            http=http,
                            market_ticker=m.ticker,
                            start_ts=event_start_ts,
                            end_ts=close_ts,
                            use_historical=(m.close_ts < cutoff_ts),
                        )
                    except Exception as e:
                        print(f"[backtest] warning: candles unavailable for {m.ticker}: {e}")
                        rows = []
                cache.write_json_gz(cpath, rows)
            cmap: Dict[int, Candle] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    ts = int(r["ts"])
                except Exception:
                    continue
                cmap[ts] = Candle(
                    ts=ts,
                    yes_bid_cents=dollars_to_cents(r.get("yes_bid_cents")),
                    yes_ask_cents=dollars_to_cents(r.get("yes_ask_cents")),
                )
            if cmap:
                candles_by_market[m.ticker] = cmap
                candle_keys_by_market[m.ticker] = sorted(cmap.keys())

        if not candles_by_market:
            continue
        events_simulated += 1

        event_close_start_idx = len(closed_trades)
        meta_by_ticker = {m.ticker: m for m in metas}
        step_s = int(bt.STEP_SECONDS) if bt.STEP_SECONDS is not None else int(bt.STEP_MINUTES) * 60
        t = int(math.ceil(event_start_ts / step_s) * step_s)
        while t < close_ts:
            minutes_left = max(0.0, float(close_ts - t) / 60.0)
            if minutes_left <= 0.0:
                break

            spot = spot_at_or_before(t)
            if spot is None:
                t += step_s
                continue
            sigma, vol_source = volatility_at(t)
            if sigma is None:
                t += step_s
                continue
            vol_source_counts[vol_source] = vol_source_counts.get(vol_source, 0) + 1

            chosen = _pick_markets(metas, spot=spot, max_strikes=int(bt.MAX_STRIKES), band_pct=float(bt.BAND_PCT))
            cands: List[_Candidate] = []
            
            # OPTIMIZATION: Generate terminal prices once per minute tick, reuse across all strikes
            hourly_vol = convert_annualized_vol_to_hourly(float(sigma))
            time_remaining_hours = float(minutes_left) / 60.0
            
            terminal_prices = simulate_t_dist_terminal_prices(
                current_price=float(spot),
                hourly_vol=hourly_vol,
                time_remaining_hours=time_remaining_hours,
                df=3.0,
                n_paths=10_000,
            )

            exited_markets: set[str] = set()
            for market_ticker, pos in list(positions.items()):
                if pos.event_ticker != et or int(pos.total_count) <= 0:
                    continue
                meta = meta_by_ticker.get(market_ticker)
                if meta is None or t < int(meta.open_ts):
                    continue
                c = _candle_at_or_before(
                    candles_by_market.get(market_ticker, {}),
                    candle_keys_by_market.get(market_ticker, []),
                    t,
                )
                if c is None:
                    continue
                ybid, yask = c.yes_bid_cents, c.yes_ask_cents
                if ybid is None and yask is None:
                    continue
                p_yes = clamp01(float(np.mean(terminal_prices > float(meta.strike))))
                nbid, nask = derive_no_quotes(ybid, yask)
                decision = evaluate_exit(
                    snapshot=ExitMarketSnapshot(
                        side=str(pos.side),
                        p_yes=float(p_yes),
                        minutes_left=float(minutes_left),
                        yes_bid_cents=ybid,
                        yes_ask_cents=yask,
                        no_bid_cents=nbid,
                        no_ask_cents=nask,
                    ),
                    total_count=int(pos.total_count),
                    total_cost_dollars=float(pos.total_cost_dollars),
                    take_profit_mid_cents=(
                        int(cfg.EXIT_TAKE_PROFIT_MID_CENTS) if cfg.EXIT_TAKE_PROFIT_MID_CENTS is not None else None
                    ),
                    exit_minutes_left=float(cfg.EXIT_MINUTES_LEFT),
                    signal_exit_enabled=bool(cfg.EXIT_ON_SIGNAL_REVERSAL),
                    signal_exit_min_edge_pp=float(cfg.EXIT_SIGNAL_MIN_EDGE_PP),
                )
                if decision is None:
                    continue
                exited_markets.add(str(market_ticker))
                if decision.bid_cents is None or int(decision.bid_cents) <= 0:
                    continue
                closed = _close_position(
                    positions=positions,
                    market_cost=market_cost,
                    event_cost=event_cost,
                    event_positions=event_positions,
                    pos=pos,
                    exit_ts=int(t),
                    exit_reason=str(decision.reason),
                    exit_price_cents=int(decision.bid_cents),
                    exit_fee_cents=int(cfg.FEE_CENTS),
                )
                closed_trades.append(closed)
                realized_pnl_total = float(realized_pnl_total + float(closed.pnl_dollars))
                _append_jsonl(
                    log_path,
                    {
                        "record_type": "exit",
                        "ts": _iso_utc(int(t)),
                        "event": closed.event_ticker,
                        "market_ticker": closed.market_ticker,
                        "side": closed.side,
                        "contracts": int(closed.contracts),
                        "reason": closed.exit_reason,
                        "exit_price_cents": int(decision.bid_cents),
                        "exit_fee_cents": int(cfg.FEE_CENTS),
                        "mid_cents": float(decision.mid_cents) if decision.mid_cents is not None else None,
                        "p_win_now": float(decision.p_win_now),
                        "avg_entry_cost_cents": float(decision.avg_entry_cost_cents),
                        "edge_now_pp": float(decision.edge_now_pp),
                        "pnl": float(closed.pnl_dollars),
                    },
                )

            if should_pause_new_entries(minutes_left=float(minutes_left), exit_minutes_left=float(cfg.EXIT_MINUTES_LEFT)):
                t += step_s
                continue

            for m in chosen:
                if t < m.open_ts:
                    continue
                c = _candle_at_or_before(
                    candles_by_market.get(m.ticker, {}),
                    candle_keys_by_market.get(m.ticker, []),
                    t,
                )
                if c is None:
                    continue
                ybid, yask = c.yes_bid_cents, c.yes_ask_cents
                if yask is None and ybid is None:
                    continue
                spread = _quote_spread(ybid, yask)
                if spread is not None and int(spread) < 0:
                    continue
                if spread is not None and int(cfg.SPREAD_MAX_CENTS) >= 0 and int(spread) > int(cfg.SPREAD_MAX_CENTS):
                    continue

                # Calculate probability from the pre-generated terminal prices
                p_yes_raw = float(np.mean(terminal_prices > float(m.strike)))
                p_yes = clamp01(p_yes_raw)

                nbid, nask = derive_no_quotes(ybid, yask)

                best: Optional[_Candidate] = None
                if _valid_taker_price_cents(yask):
                    max_yes = max_acceptable_price_cents(
                        p_win=p_yes,
                        min_ev=float(cfg.MIN_EV),
                        fee_buffer_cents=int(cfg.FEE_CENTS),
                    )
                    if int(yask) <= int(max_yes):
                        ev_yes = edge_at_price(p_win=p_yes, price_cents=int(yask), fee_cents=int(cfg.FEE_CENTS))
                        best = _Candidate(
                            event_ticker=et,
                            market_ticker=m.ticker,
                            strike=m.strike,
                            side="yes",
                            price_cents=int(yask),
                            fee_cents=int(cfg.FEE_CENTS),
                            p_yes=float(p_yes),
                            edge_pp=float(ev_yes),
                            spread=spread,
                            ts=t,
                        )
                if _valid_taker_price_cents(nask):
                    p_no = 1.0 - p_yes
                    max_no = max_acceptable_price_cents(
                        p_win=p_no,
                        min_ev=float(cfg.MIN_EV),
                        fee_buffer_cents=int(cfg.FEE_CENTS),
                    )
                    if int(nask) <= int(max_no):
                        ev_no = edge_at_price(p_win=p_no, price_cents=int(nask), fee_cents=int(cfg.FEE_CENTS))
                        cand_no = _Candidate(
                            event_ticker=et,
                            market_ticker=m.ticker,
                            strike=m.strike,
                            side="no",
                            price_cents=int(nask),
                            fee_cents=int(cfg.FEE_CENTS),
                            p_yes=float(p_yes),
                            edge_pp=float(ev_no),
                            spread=spread,
                            ts=t,
                        )
                        if best is None or float(cand_no.edge_pp) > float(best.edge_pp):
                            best = cand_no
                if best is not None:
                    cands.append(best)

            cands.sort(key=lambda x: float(x.edge_pp), reverse=True)
            for cand in cands:
                if cand.market_ticker in exited_markets:
                    continue
                pos = positions.get(cand.market_ticker)
                current = int(pos.total_count) if pos else 0
                existing_side = pos.side if pos else None
                if existing_side is not None and existing_side != cand.side:
                    continue
                
                if float(cand.edge_pp) < float(cfg.MIN_EV):
                    continue

                bankroll_dollars = max(0.0, float(bt.STARTING_BANKROLL_DOLLARS) + float(realized_pnl_total))
                total_open_cost = float(sum(float(v) for v in market_cost.values()))
                target = desired_total_contracts(
                    sizing_mode=str(bt.POSITION_SIZING_MODE),
                    order_size=int(cfg.ORDER_SIZE),
                    max_contracts_per_market=int(cfg.MAX_CONTRACTS_PER_MARKET),
                    price_cents=int(cand.price_cents),
                    fee_cents=int(cand.fee_cents),
                    p_win=float(cand.p_yes if cand.side == "yes" else (1.0 - cand.p_yes)),
                    bankroll_dollars=float(bankroll_dollars),
                    kelly_fraction_scale=float(bt.KELLY_FRACTION),
                    current_contracts=int(current),
                )
                add_count = int(target - current)
                if add_count <= 0:
                    continue

                cost_per_contract = float(cand.price_cents + cand.fee_cents) / 100.0
                available_cash = max(0.0, float(bankroll_dollars) - float(total_open_cost))
                if cost_per_contract > 0:
                    add_count = min(int(add_count), int(math.floor(available_cash / cost_per_contract)))
                if add_count <= 0:
                    continue

                add_cost = float(add_count) * float(cost_per_contract)
                is_new_market = current <= 0
                if is_new_market and int(event_positions.get(et, 0)) >= int(cfg.MAX_POSITIONS_PER_EVENT):
                    continue
                if float(event_cost.get(et, 0.0)) + add_cost > float(cfg.MAX_COST_PER_EVENT):
                    continue
                if float(market_cost.get(cand.market_ticker, 0.0)) + add_cost > float(cfg.MAX_COST_PER_MARKET):
                    continue

                if pos is None:
                    pos = Position(
                        event_ticker=et,
                        market_ticker=cand.market_ticker,
                        side=cand.side,
                    )
                    positions[cand.market_ticker] = pos

                fill = Fill(
                    ts=cand.ts,
                    event_ticker=et,
                    market_ticker=cand.market_ticker,
                    side=cand.side,
                    contracts=add_count,
                    entry_price_cents=int(cand.price_cents),
                    fee_cents=int(cand.fee_cents),
                    p_yes=float(cand.p_yes),
                    p_win=float(cand.p_yes if cand.side == "yes" else (1.0 - cand.p_yes)),
                    ev=float(cand.edge_pp),
                    spread=cand.spread,
                    sigma=float(sigma),
                    vol_source=vol_source,
                )
                all_fills.append(fill)
                pos.fills.append(fill)
                pos.total_count += add_count
                pos.total_cost_dollars += add_cost
                pos.total_fee_dollars += float(add_count) * (float(cand.fee_cents) / 100.0)
                pos.last_fill_ts = cand.ts
                market_cost[cand.market_ticker] = float(market_cost.get(cand.market_ticker, 0.0) + add_cost)
                event_cost[et] = float(event_cost.get(et, 0.0) + add_cost)
                if is_new_market:
                    event_positions[et] = int(event_positions.get(et, 0) + 1)

                _append_jsonl(
                    log_path,
                    {
                        "record_type": "entry",
                        "ts": _iso_utc(fill.ts),
                        "event": fill.event_ticker,
                        "market_ticker": fill.market_ticker,
                        "side": fill.side,
                        "contracts": int(fill.contracts),
                        "entry_price_cents": int(fill.entry_price_cents),
                        "fee_cents": int(fill.fee_cents),
                        "p_yes": float(fill.p_yes),
                        "p_win": float(fill.p_win),
                        "ev": float(fill.ev),
                        "spread": int(fill.spread) if fill.spread is not None else None,
                        "sigma": float(fill.sigma),
                        "vol_source": fill.vol_source,
                        "position_sizing_mode": str(bt.POSITION_SIZING_MODE),
                        "bankroll_dollars": float(bankroll_dollars),
                        "kelly_fraction": (
                            float(
                                kelly_fraction_binary(
                                    p_win=float(fill.p_win),
                                    total_cost_dollars=float(fill.entry_price_cents + fill.fee_cents) / 100.0,
                                )
                            )
                            if str(bt.POSITION_SIZING_MODE) == "kelly"
                            else None
                        ),
                    },
                )
            t += step_s

        for market_ticker, pos in list(positions.items()):
            if pos.event_ticker != et or int(pos.total_count) <= 0:
                continue
            m = meta_by_ticker.get(market_ticker)
            result = m.result if m is not None else None
            settlement_value = m.settlement_value if m is not None else None
            if str(result or "").lower() == "void":
                payout_per = _position_avg_principal_cents(pos)
            else:
                payout_per = _payout_cents_per_contract(
                    result=result,
                    side=pos.side,
                    settlement_value=settlement_value,
                    entry_price_cents=_position_avg_principal_cents(pos),
                )
            if payout_per is None:
                continue
            closed = _close_position(
                positions=positions,
                market_cost=market_cost,
                event_cost=event_cost,
                event_positions=event_positions,
                pos=pos,
                exit_ts=int(close_ts),
                exit_reason="settlement",
                exit_price_cents=int(payout_per),
                exit_fee_cents=0,
            )
            closed_trades.append(closed)
            realized_pnl_total = float(realized_pnl_total + float(closed.pnl_dollars))
            _append_jsonl(
                log_path,
                {
                    "record_type": "exit",
                    "ts": _iso_utc(int(close_ts)),
                    "event": closed.event_ticker,
                    "market_ticker": closed.market_ticker,
                    "side": closed.side,
                    "contracts": int(closed.contracts),
                    "reason": "settlement",
                    "exit_price_cents": int(payout_per),
                    "exit_fee_cents": 0,
                    "pnl": float(closed.pnl_dollars),
                },
            )

        event_closed = [c for c in closed_trades[event_close_start_idx:] if c.event_ticker == et]
        pnl = 0.0
        wins = 0
        settled = 0
        for closed in event_closed:
            settled += 1
            pnl += float(closed.pnl_dollars)
            if float(closed.pnl_dollars) > 0:
                wins += 1

        total_wins += wins
        total_settled += settled

        trades = len(event_closed)
        contracts = sum(int(c.contracts) for c in event_closed)
        event_win_rate = (float(wins) / float(settled)) if settled > 0 else 0.0
        er = EventResult(
            event_ticker=et,
            close_ts=close_ts,
            trades=trades,
            contracts=contracts,
            pnl=float(pnl),
            win_rate=float(event_win_rate),
        )
        per_event_results.append(er)
        _append_jsonl(
            log_path,
            {
                "record_type": "event_summary",
                "event": er.event_ticker,
                "close_ts": _iso_utc(er.close_ts),
                "trades": int(er.trades),
                "contracts": int(er.contracts),
                "pnl": float(er.pnl),
                "win_rate": float(er.win_rate),
            },
        )

    total_pnl = float(sum(float(e.pnl) for e in per_event_results))
    total_trades = int(sum(int(e.trades) for e in per_event_results))
    total_contracts = int(sum(int(e.contracts) for e in per_event_results))
    win_rate = (float(total_wins) / float(total_settled)) if total_settled > 0 else 0.0

    total_vol_ticks = sum(vol_source_counts.values()) if vol_source_counts else 0
    vol_summary_parts = []
    for src in ("regression", "garch", "weighted_proxy", "rv_fallback"):
        cnt = vol_source_counts.get(src, 0)
        if cnt > 0:
            pct = 100.0 * cnt / total_vol_ticks
            vol_summary_parts.append(f"{src}={cnt} ({pct:.1f}%)")
    print(
        f"[backtest] vol sources: {', '.join(vol_summary_parts) or 'none'} | "
        f"dvol_hours={len(dvol_by_hour)} rv_hours={len(rv_by_hour)} | "
        f"GARCH cache: {garch_hits} hits, {garch_misses} fits"
    )

    summary = BacktestSummary(
        series_ticker=str(bt.SERIES_TICKER),
        start_ts=start_ts,
        end_ts=end_ts,
        events_scanned=int(events_scanned),
        events_simulated=int(events_simulated),
        trades=total_trades,
        contracts=total_contracts,
        total_pnl=float(total_pnl),
        win_rate=float(win_rate),
        per_event=per_event_results,
        log_path=str(log_path),
    )

    _append_jsonl(
        log_path,
        {
            "record_type": "run_summary",
            "series_ticker": str(summary.series_ticker),
            "start_ts": _iso_utc(summary.start_ts),
            "end_ts": _iso_utc(summary.end_ts),
            "events_scanned": int(summary.events_scanned),
            "events_simulated": int(summary.events_simulated),
            "trades": int(summary.trades),
            "contracts": int(summary.contracts),
            "total_pnl": float(summary.total_pnl),
            "win_rate": float(summary.win_rate),
            "vol_source_counts": dict(vol_source_counts),
            "dvol_hours_available": len(dvol_by_hour),
            "rv_hours_available": len(rv_by_hour),
            "garch_cache_hits": garch_hits,
            "garch_cache_fits": garch_misses,
            "config": {
                "strategy": config_to_dict(cfg),
                "backtest": dict(bt.__dict__),
                "notes": {
                    "fill_model": "taker-only: entries at ask, exits at bid or settlement",
                    "vol_model": "regression (weighted implied proxy + RV) > GARCH(1,1) > weighted implied proxy > trailing RV",
                    "regression_lookback": f"{_REGRESSION_LOOKBACK_HOURS}h, min 24 obs, no lookahead",
                    "rv_window_minutes": int(bt.REALIZED_VOL_WINDOW_MINUTES),
                    "position_sizing_mode": str(bt.POSITION_SIZING_MODE),
                    "starting_bankroll_dollars": float(bt.STARTING_BANKROLL_DOLLARS),
                    "kelly_fraction": float(bt.KELLY_FRACTION),
                    "step_seconds": int(bt.STEP_SECONDS) if bt.STEP_SECONDS is not None else int(bt.STEP_MINUTES) * 60,
                    "depth_gate": "MIN_TOP_SIZE ignored (no orderbook size in candles)",
                    "subminute_note": (
                        "sub-minute cadence reuses the latest available 1m candle until a fresh candle arrives"
                        if ((int(bt.STEP_SECONDS) if bt.STEP_SECONDS is not None else int(bt.STEP_MINUTES) * 60) < 60)
                        else None
                    ),
                },
            },
        },
    )
    return summary
