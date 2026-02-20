"""
settlement_tracker.py

Best-effort settlement/outcome logging:
- polls Kalshi public event endpoint
- emits one `event_settled` record per event per run when we detect settlement

This is intentionally heuristic: Kalshi response fields can vary, so we log what we can.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_api import get_event
from kalshi_edge.trade_log import TradeLogger, utc_ts


def _read_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _event_status(event_json: Dict[str, Any]) -> str:
    event = event_json.get("event") if isinstance(event_json.get("event"), dict) else {}
    s = str(event.get("status") or event.get("settlement_status") or event.get("state") or "")
    return s.lower()


def _looks_settled(event_json: Dict[str, Any]) -> bool:
    event = event_json.get("event") if isinstance(event_json.get("event"), dict) else {}
    if bool(event.get("is_settled")) is True:
        return True
    status = _event_status(event_json)
    if status in {"settled", "settlement", "resolved", "closed", "finalized", "final", "complete", "completed"}:
        return True
    markets = event_json.get("markets") or event.get("markets") or []
    if isinstance(markets, list) and markets:
        known = 0
        for m in markets[:2000]:
            if not isinstance(m, dict):
                continue
            if _market_payout_yes(m) is not None:
                known += 1
        # If we can read outcomes for many markets, treat it as settled.
        if known >= max(1, int(0.6 * len([m for m in markets if isinstance(m, dict)]))):
            return True
    return False


def _market_payout_yes(market: Dict[str, Any]) -> Optional[float]:
    """
    Return payout for a YES share (1.0 or 0.0) if we can infer it; otherwise None.
    """
    for k in ("result", "winning_outcome", "settlement_outcome", "outcome", "settled_outcome"):
        v = market.get(k)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"yes", "y"}:
                return 1.0
            if s in {"no", "n"}:
                return 0.0

    for k in ("settlement_value", "settlement_value_yes", "yes_settlement_value", "settlement_price"):
        v = market.get(k)
        if isinstance(v, (int, float)):
            if float(v) in (0.0, 1.0):
                return float(v)

    # Sometimes Kalshi uses per-outcome settlement prices.
    y = market.get("yes_settlement_price")
    n = market.get("no_settlement_price")
    if isinstance(y, (int, float)) and isinstance(n, (int, float)):
        if float(y) in (0.0, 1.0) and float(n) in (0.0, 1.0) and float(y) + float(n) == 1.0:
            return float(y)

    return None


@dataclass
class SettlementResult:
    is_settled: bool
    event_status: str
    payout_yes_by_market: Dict[str, float]
    outcome_raw: Dict[str, Any]


def _extract_settlement(event_json: Dict[str, Any]) -> SettlementResult:
    event = event_json.get("event") if isinstance(event_json.get("event"), dict) else {}
    markets = event_json.get("markets") or event.get("markets") or []
    payout_yes_by_market: Dict[str, float] = {}
    outcome_rows = []
    if isinstance(markets, list):
        for m in markets[:5000]:
            if not isinstance(m, dict):
                continue
            tkr = m.get("ticker") or m.get("market_ticker")
            if not isinstance(tkr, str):
                continue
            py = _market_payout_yes(m)
            if py is None:
                continue
            payout_yes_by_market[str(tkr)] = float(py)
            outcome_rows.append({"market_ticker": str(tkr), "payout_yes": float(py), "result": m.get("result")})

    status = _event_status(event_json)
    outcome_raw = {
        "event": {
            "event_ticker": event.get("ticker") or event.get("event_ticker"),
            "status": event.get("status") or event.get("settlement_status") or event.get("state"),
            "close_time": event.get("close_time"),
            "settlement_time": event.get("settlement_time") or event.get("settled_time"),
        },
        # Keep small: at most 50 markets.
        "markets": outcome_rows[:50],
    }
    return SettlementResult(is_settled=_looks_settled(event_json), event_status=status, payout_yes_by_market=payout_yes_by_market, outcome_raw=outcome_raw)


class SettlementTracker:
    def __init__(self, *, http: HttpClient):
        self.http = http
        self._emitted: Set[str] = set()

    def maybe_log_settlements(self, *, trader: Any, log: Any, active_event_ticker: str) -> None:
        if not isinstance(active_event_ticker, str) or not active_event_ticker:
            return
        if not isinstance(log, TradeLogger):
            return

        event_tickers: Set[str] = {str(active_event_ticker).upper()}

        # v2: include any events we have positions in
        ops = getattr(trader, "open_positions", None)
        if isinstance(ops, dict):
            for _mkt, pos in ops.items():
                if not isinstance(pos, dict):
                    continue
                evt = pos.get("event_ticker")
                if isinstance(evt, str) and evt:
                    event_tickers.add(evt.upper())

        # v1: include state-file event (if any)
        st_path = getattr(trader, "state_file", None)
        if isinstance(st_path, str) and st_path:
            st = _read_json(st_path)
            evt = st.get("event_ticker")
            if isinstance(evt, str) and evt:
                event_tickers.add(evt.upper())

        for evt in sorted(event_tickers):
            if evt in self._emitted:
                continue
            self._maybe_emit_for_event(event_ticker=evt, trader=trader, log=log)

    def _maybe_emit_for_event(self, *, event_ticker: str, trader: Any, log: TradeLogger) -> None:
        event_ticker = str(event_ticker).upper()
        event_json = get_event(self.http, event_ticker)
        settle = _extract_settlement(event_json)
        if not settle.is_settled:
            return

        pnl_total = None
        pnl_by_market: Dict[str, float] = {}
        pnl_notes = []

        # v2 best-effort: use open_positions totals
        ops = getattr(trader, "open_positions", None)
        if isinstance(ops, dict):
            for mkt, pos in ops.items():
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("event_ticker") or "").upper() != event_ticker:
                    continue
                side = str(pos.get("side") or "").lower()
                if side not in {"yes", "no"}:
                    continue
                tc = pos.get("total_count")
                cost = pos.get("total_cost_dollars")
                fee = pos.get("total_fee_dollars")
                try:
                    count = int(tc)
                    total_cost = float(cost)
                    total_fee = float(fee)
                except Exception:
                    continue

                py = settle.payout_yes_by_market.get(str(mkt))
                if py is None:
                    pnl_notes.append(f"no_outcome_for_market:{mkt}")
                    continue
                payout = float(py) if side == "yes" else float(1.0 - py)
                pnl = float(count) * payout - float(total_cost) - float(total_fee)
                pnl_by_market[str(mkt)] = float(pnl)

        # v1 best-effort: state-file single position
        st_path = getattr(trader, "state_file", None)
        if isinstance(st_path, str) and st_path and not pnl_by_market:
            st = _read_json(st_path)
            if bool(st.get("open")) and str(st.get("event_ticker") or "").upper() == event_ticker:
                mkt = st.get("market_ticker")
                side = str(st.get("side") or "").lower()
                if isinstance(mkt, str) and side in {"yes", "no"}:
                    py = settle.payout_yes_by_market.get(str(mkt))
                    if py is None:
                        pnl_notes.append(f"no_outcome_for_market:{mkt}")
                    else:
                        payout = float(py) if side == "yes" else float(1.0 - py)
                        try:
                            count = int(st.get("position_count") or 0)
                            entry_cost = float(st.get("entry_cost"))
                        except Exception:
                            count = 0
                            entry_cost = 0.0
                        if count > 0 and entry_cost > 0:
                            pnl = float(count) * (payout - float(entry_cost))
                            pnl_by_market[str(mkt)] = float(pnl)
                        else:
                            pnl_notes.append("v1_state_missing_entry_cost_or_count")

        if pnl_by_market:
            pnl_total = float(sum(pnl_by_market.values()))
        else:
            pnl_notes.append("no_positions_pnl_computed")

        log.log(
            "event_settled",
            {
                "event_ticker": event_ticker,
                "settled_ts_utc": utc_ts(),
                "event_status": str(settle.event_status),
                "outcome_raw": settle.outcome_raw,
                "pnl_total": pnl_total,
                "pnl_by_market": pnl_by_market or None,
                "pnl_notes": pnl_notes or None,
            },
        )
        self._emitted.add(event_ticker)

