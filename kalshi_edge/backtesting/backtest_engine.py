"""
Minute-cadence backtest engine for Kalshi BTC ladder events.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import statistics
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
from kalshi_edge.math_models import clamp01, lognormal_prob_above
from kalshi_edge.strategy_config import BacktestConfig, StrategyConfig, config_to_dict
from kalshi_edge.util.time import parse_iso8601


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
    if len(rets) < 2:
        return 0.0
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
        events_raw = events_raw[: int(bt.MAX_EVENTS)]

    coinbase_cache_path = cache.coinbase_candles_path("BTC-USD", start_ts, end_ts)
    coin_rows = cache.read_json_gz(coinbase_cache_path)
    if not isinstance(coin_rows, list):
        coin_rows = fetch_coinbase_candles_1m(http=http, start_ts=start_ts, end_ts=end_ts, product="BTC-USD")
        cache.write_json_gz(coinbase_cache_path, coin_rows)
    close_by_ts = build_close_by_minute_ts(coin_rows)
    close_keys = sorted(close_by_ts.keys())
    close_vals = [float(close_by_ts[k]) for k in close_keys]

    def spot_at_or_before(ts: int) -> Optional[float]:
        idx = bisect.bisect_right(close_keys, int(ts)) - 1
        if idx < 0:
            return None
        return float(close_vals[idx])

    def realized_vol_at(ts: int, window: int = 61) -> Optional[float]:
        idx = bisect.bisect_right(close_keys, int(ts)) - 1
        if idx < 0:
            return None
        lo = max(0, idx - int(window) + 1)
        closes = close_vals[lo : idx + 1]
        if len(closes) < 2:
            return None
        return annualized_realized_vol_from_closes(closes)

    positions: Dict[str, Position] = {}
    market_cost: Dict[str, float] = {}
    event_cost: Dict[str, float] = {}
    event_positions: Dict[str, int] = {}
    all_fills: List[Fill] = []
    per_event_results: List[EventResult] = []
    events_scanned = 0
    events_simulated = 0

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
            # "today at Xpm" events are day-scoped; 24h lookback keeps runtime predictable.
            event_start_ts = max(start_ts, close_ts - 24 * 60 * 60)
        if event_start_ts >= close_ts:
            continue

        candles_by_market: Dict[str, Dict[int, Candle]] = {}
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
                        # Keep backtest robust: skip market on fetch errors.
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

        if not candles_by_market:
            continue
        events_simulated += 1

        event_fill_start_idx = len(all_fills)
        step_s = int(bt.STEP_MINUTES) * 60
        t = int(math.ceil(event_start_ts / step_s) * step_s)
        while t < close_ts:
            minutes_left = max(0.0, float(close_ts - t) / 60.0)
            if minutes_left <= 0.0:
                break

            spot = spot_at_or_before(t)
            if spot is None:
                t += step_s
                continue
            sigma = realized_vol_at(t)
            if sigma is None:
                t += step_s
                continue

            chosen = _pick_markets(metas, spot=spot, max_strikes=int(bt.MAX_STRIKES), band_pct=float(bt.BAND_PCT))
            cands: List[_Candidate] = []
            for m in chosen:
                c = candles_by_market.get(m.ticker, {}).get(t)
                if c is None:
                    continue
                ybid, yask = c.yes_bid_cents, c.yes_ask_cents
                if yask is None and ybid is None:
                    continue
                spread = _quote_spread(ybid, yask)
                # Skip crossed/inverted quotes from sparse candle snapshots.
                if spread is not None and int(spread) < 0:
                    continue
                if spread is not None and int(cfg.SPREAD_MAX_CENTS) >= 0 and int(spread) > int(cfg.SPREAD_MAX_CENTS):
                    continue

                p_yes = clamp01(lognormal_prob_above(float(spot), float(m.strike), float(sigma), float(minutes_left)))
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
            entries_this_tick = 0
            for cand in cands:
                if entries_this_tick >= int(cfg.MAX_ENTRIES_PER_TICK):
                    break
                pos = positions.get(cand.market_ticker)
                current = int(pos.total_count) if pos else 0
                existing_side = pos.side if pos else None
                if existing_side is not None and existing_side != cand.side:
                    continue
                if current > 0 and bool(cfg.DEDUPE_MARKETS):
                    continue
                if current <= 0:
                    if float(cand.edge_pp) < float(cfg.MIN_EV):
                        continue
                else:
                    if not bool(cfg.ALLOW_SCALE_IN):
                        continue
                    if float(cand.edge_pp) < float(cfg.SCALE_IN_MIN_EV):
                        continue
                    if pos is not None and pos.last_fill_ts is not None:
                        if (cand.ts - int(pos.last_fill_ts)) < int(cfg.SCALE_IN_COOLDOWN_SECONDS):
                            continue

                target = min(int(cfg.MAX_CONTRACTS_PER_MARKET), current + int(cfg.ORDER_SIZE))
                add_count = int(target - current)
                if add_count <= 0:
                    continue

                add_cost = float(add_count) * (float(cand.price_cents + cand.fee_cents) / 100.0)
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
                entries_this_tick += 1

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
                    },
                )
            t += step_s

        event_fills = [f for f in all_fills[event_fill_start_idx:] if f.event_ticker == et]
        pnl = 0.0
        wins = 0
        settled = 0
        meta_by_ticker = {m.ticker: m for m in metas}
        for f in event_fills:
            m = meta_by_ticker.get(f.market_ticker)
            result = m.result if m is not None else None
            settlement_value = m.settlement_value if m is not None else None
            payout_per = _payout_cents_per_contract(
                result=result,
                side=f.side,
                settlement_value=settlement_value,
                entry_price_cents=f.entry_price_cents,
            )
            if payout_per is None:
                continue
            settled += 1
            payout_total = int(payout_per) * int(f.contracts)
            cost_total = int(f.entry_price_cents + f.fee_cents) * int(f.contracts)
            fill_pnl = float(payout_total - cost_total) / 100.0
            pnl += fill_pnl
            if fill_pnl > 0:
                wins += 1

        trades = len(event_fills)
        contracts = sum(int(f.contracts) for f in event_fills)
        win_rate = (float(wins) / float(settled)) if settled > 0 else 0.0
        er = EventResult(
            event_ticker=et,
            close_ts=close_ts,
            trades=trades,
            contracts=contracts,
            pnl=float(pnl),
            win_rate=float(win_rate),
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
    settled_events = [e for e in per_event_results if e.trades > 0]
    win_rate = 0.0
    if settled_events:
        win_rate = float(sum(float(e.win_rate) for e in settled_events) / float(len(settled_events)))

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
            "config": {
                "strategy": config_to_dict(cfg),
                "backtest": dict(bt.__dict__),
                "notes": {
                    "fill_model": "immediate taker fill at ask when quote present",
                    "depth_gate": "MIN_TOP_SIZE ignored in backtest (no orderbook size in candles)",
                },
            },
        },
    )
    return summary
