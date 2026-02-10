"""
trader_v0.py

V0 trading loop:
  - Evaluate the current KXBTCD ABOVE ladder
  - Pick the single best contract by EV% (EV / cost)
  - If EV% >= threshold, place a Fill-or-Kill limit buy
  - If it fills, persist a small state file and stop trading once the max-contract cap is reached

This is intentionally simple: no exit logic; hold to settlement.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_api import create_order, get_positions
from kalshi_edge.pipeline import EvaluationResult


@dataclass
class TradeCandidate:
    market_ticker: str
    side: str  # "yes" or "no"
    buy_price_cents: int
    ev_dollars: float
    ev_pct: float
    p_model: float
    strike: float
    subtitle: str
    spread_cents: Optional[int]
    depth: float


def _candidate_from_row(row, fee_cents: int) -> Tuple[Optional[TradeCandidate], Optional[TradeCandidate]]:
    """Return (yes_candidate, no_candidate) for a LadderRow."""
    out_yes = None
    out_no = None

    if row.ob.ybuy is not None and row.ev_yes is not None:
        cost = (row.ob.ybuy + fee_cents) / 100.0
        if cost > 0:
            out_yes = TradeCandidate(
                market_ticker=row.ticker,
                side="yes",
                buy_price_cents=int(row.ob.ybuy),
                ev_dollars=float(row.ev_yes),
                ev_pct=float(row.ev_yes) / cost,
                p_model=float(row.p_model),
                strike=float(row.strike),
                subtitle=str(row.subtitle),
                spread_cents=row.ob.spread_y,
                depth=float(row.ob.depth_y),
            )

    if row.ob.nbuy is not None and row.ev_no is not None:
        cost = (row.ob.nbuy + fee_cents) / 100.0
        if cost > 0:
            out_no = TradeCandidate(
                market_ticker=row.ticker,
                side="no",
                buy_price_cents=int(row.ob.nbuy),
                ev_dollars=float(row.ev_no),
                ev_pct=float(row.ev_no) / cost,
                p_model=float(row.p_model),
                strike=float(row.strike),
                subtitle=str(row.subtitle),
                spread_cents=row.ob.spread_n,
                depth=float(row.ob.depth_n),
            )

    return out_yes, out_no


def pick_best_candidate(
    result: EvaluationResult,
    *,
    fee_cents: int,
    min_ev_pct: float,
    min_minutes_left: float,
    max_spread_cents: Optional[int] = None,
    min_depth: float = 0.0,
) -> Optional[TradeCandidate]:
    """Pick the best single trade by EV% that passes basic filters."""
    if result.minutes_left < min_minutes_left:
        return None

    best: Optional[TradeCandidate] = None

    for row in result.rows:
        c_yes, c_no = _candidate_from_row(row, fee_cents=fee_cents)
        for c in (c_yes, c_no):
            if c is None:
                continue
            if c.ev_pct < min_ev_pct:
                continue
            if c.depth < min_depth:
                continue
            if max_spread_cents is not None and c.spread_cents is not None and c.spread_cents > max_spread_cents:
                continue
            if best is None or c.ev_pct > best.ev_pct:
                best = c

    return best


def _read_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_state(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _filled_contracts_from_state(st: Dict[str, Any]) -> int:
    if isinstance(st.get("filled_contracts"), int):
        return int(st["filled_contracts"])
    if st.get("open") and isinstance(st.get("count"), int):
        return int(st["count"])
    return 0


class V0Trader:
    def __init__(
        self,
        *,
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        fee_cents: int,
        min_ev_pct: float = 0.05,
        count: int = 1,
        max_contracts: Optional[int] = None,
        min_minutes_left: float = 2.0,
        max_spread_cents: Optional[int] = None,
        min_depth: float = 0.0,
        dry_run: bool = False,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.fee_cents = fee_cents
        self.min_ev_pct = min_ev_pct
        self.count = count
        self.max_contracts = max_contracts
        self.min_minutes_left = min_minutes_left
        self.max_spread_cents = max_spread_cents
        self.min_depth = min_depth
        self.dry_run = dry_run
        self._cap_logged = False

    def _filled_contracts(self) -> int:
        st = _read_state(self.state_file)
        return _filled_contracts_from_state(st)

    def _remaining_contracts(self) -> Optional[int]:
        if self.max_contracts is None:
            return None
        return max(int(self.max_contracts) - self._filled_contracts(), 0)

    def reconcile_state(self, event_ticker: str) -> None:
        """Sync state file against live portfolio positions for the given event."""
        event_ticker = event_ticker.upper()
        cursor = None
        market_positions = []
        try:
            while True:
                resp = get_positions(
                    self.http,
                    self.auth,
                    base_url=self.kalshi_base_url,
                    event_ticker=event_ticker,
                    limit=1000,
                    cursor=cursor,
                    count_filter="position",
                )
                market_positions.extend(resp.get("market_positions") or [])
                cursor = resp.get("cursor")
                if not cursor:
                    break
        except Exception as e:
            print(f"[TRADE] reconcile failed: {e}")
            return

        total = 0
        top_ticker = None
        top_position = 0
        for mp in market_positions:
            pos = mp.get("position")
            if pos is None:
                continue
            try:
                pos_val = int(pos)
            except Exception:
                continue
            abs_pos = abs(pos_val)
            if abs_pos <= 0:
                continue
            total += abs_pos
            if abs_pos > abs(top_position):
                top_position = pos_val
                top_ticker = mp.get("ticker") or mp.get("market_ticker")

        ts = datetime.now(timezone.utc).isoformat()
        if total <= 0:
            _write_state(
                self.state_file,
                {
                    "open": False,
                    "event_ticker": event_ticker,
                    "filled_contracts": 0,
                    "reconciled_at": ts,
                },
            )
            print(f"[TRADE] reconcile: no open positions for {event_ticker}. state cleared.")
            return

        side = "unknown"
        if top_position > 0:
            side = "yes"
        elif top_position < 0:
            side = "no"

        _write_state(
            self.state_file,
            {
                "open": True,
                "event_ticker": event_ticker,
                "market_ticker": top_ticker,
                "side": side,
                "count": int(total),
                "filled_contracts": int(total),
                "reconciled_at": ts,
            },
        )
        print(f"[TRADE] reconcile: {total} contracts open for {event_ticker}.")

    def maybe_trade(self, result: EvaluationResult) -> Optional[Dict[str, Any]]:
        """If a candidate clears thresholds, place a FoK order. Returns order JSON if filled."""
        remaining = self._remaining_contracts()
        if remaining == 0:
            if not self._cap_logged:
                print(f"[TRADE] max contracts reached ({self.max_contracts}); trading disabled.")
                self._cap_logged = True
            return None

        cand = pick_best_candidate(
            result,
            fee_cents=self.fee_cents,
            min_ev_pct=self.min_ev_pct,
            min_minutes_left=self.min_minutes_left,
            max_spread_cents=self.max_spread_cents,
            min_depth=self.min_depth,
        )
        if cand is None:
            return None

        order_count = int(self.count)
        if remaining is not None:
            order_count = min(order_count, int(remaining))
            if order_count <= 0:
                return None

        client_order_id = str(uuid.uuid4())
        order_data: Dict[str, Any] = {
            "ticker": cand.market_ticker,
            "action": "buy",
            "side": cand.side,
            "count": int(order_count),
            "type": "limit",
            "time_in_force": "fill_or_kill",
            "client_order_id": client_order_id,
        }
        if cand.side == "yes":
            order_data["yes_price"] = int(cand.buy_price_cents)
        else:
            order_data["no_price"] = int(cand.buy_price_cents)

        print(
            f"[TRADE] candidate ticker={cand.market_ticker} side={cand.side} price={cand.buy_price_cents}c "
            f"EV=${cand.ev_dollars:.4f} EV%={cand.ev_pct*100:.2f}% p={cand.p_model:.3f} "
            f"mins_left={result.minutes_left:.1f} spread={cand.spread_cents} depth={cand.depth:.2f}"
        )

        if self.dry_run:
            print("[TRADE] dry-run: not sending order")
            return None

        try:
            resp = create_order(
                self.http,
                self.auth,
                order_data,
                base_url=self.kalshi_base_url,
            )
        except Exception as e:
            print(f"[TRADE] order failed: {e}")
            return None

        order = resp.get("order") or resp

        fill_count = order.get("fill_count")
        status = str(order.get("status", "")).lower()
        filled = (isinstance(fill_count, int) and fill_count >= int(order_count)) or (status in {"executed", "filled"})

        if filled:
            prev_filled = self._filled_contracts()
            filled_now = None
            if isinstance(fill_count, int):
                filled_now = int(fill_count)
            else:
                try:
                    if fill_count is not None:
                        filled_now = int(fill_count)
                except Exception:
                    filled_now = None
            if filled_now is None:
                filled_now = int(order_count)
            new_total = prev_filled + int(filled_now)
            _write_state(
                self.state_file,
                {
                    "open": True,
                    "event_ticker": result.event_ticker,
                    "market_ticker": cand.market_ticker,
                    "side": cand.side,
                    "count": int(order_count),
                    "filled_contracts": int(new_total),
                    "buy_price_cents": int(cand.buy_price_cents),
                    "client_order_id": client_order_id,
                    "order_id": order.get("order_id"),
                },
            )
            print(f"[TRADE] filled. state written -> {self.state_file}")
            return order

        print(f"[TRADE] not filled (status={order.get('status')}, fill_count={fill_count}).")
        return None
