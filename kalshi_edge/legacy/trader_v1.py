"""
trader_v1.py  [DEPRECATED — use trader_v2_engine.V2Trader]

Legacy V1 trader. Migrate to V2Trader (trader_v2_engine.py) for new work.

- Entry: choose best market/side by positive edge in probability points (pp)
- Exit: take-profit / stop-loss based on capturing a fraction of the entry edge
        + time-stop + optional "edge flip"
- State I/O via telemetry.state_io (shared with other engines)
"""

from __future__ import annotations

import json
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

warnings.warn(
    "trader_v1 is deprecated; use kalshi_edge.trader_v2_engine.V2Trader instead.",
    DeprecationWarning,
    stacklevel=2,
)

from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.data.kalshi.client import (
    create_order,
    get_positions,
    get_orderbook,
    get_event,
)
from kalshi_edge.data.kalshi.models import above_markets_from_event
from kalshi_edge.ladder_eval import parse_orderbook_stats
from kalshi_edge.math_models import clamp01, lognormal_prob_above
from kalshi_edge.pipeline import EvaluationResult
from kalshi_edge.trade_log import TradeLogger
from kalshi_edge.telemetry.state_io import read_state as _read_state, write_state as _write_state
from kalshi_edge.util.time import utc_ts as _utc_ts


# _read_state, _write_state -> kalshi_edge.telemetry.state_io
# _utc_ts -> kalshi_edge.util.time


def _is_filled(resp: Dict[str, Any], want_count: int) -> bool:
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    if order is None:
        return False
    status = str(order.get("status", "")).lower()
    fill_count = order.get("fill_count")
    if isinstance(fill_count, int) and fill_count >= int(want_count):
        return True
    return status in {"executed", "filled"}


def _filled_contracts_from_state(st: Dict[str, Any]) -> int:
    if isinstance(st.get("filled_contracts"), int):
        return int(st["filled_contracts"])
    return 0


def _open_count_from_state(st: Dict[str, Any]) -> int:
    if isinstance(st.get("position_count"), int):
        return int(st["position_count"])
    if st.get("open") and isinstance(st.get("count"), int):
        return int(st["count"])
    return 0


# --------------------
# entry candidate
# --------------------

@dataclass
class EntryCandidate:
    market_ticker: str
    side: str          # "yes" or "no"
    buy_cents: int
    edge_pp: float     # dollars on $1 binary; 0.05 == 5pp
    strike: float
    p_model: float     # p(ABOVE strike)
    subtitle: str


# --------------------
# trader
# --------------------

class V1Trader:
    def __init__(
        self,
        *,
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        fee_cents: int = 1,
        count: int = 1,
        max_contracts: Optional[int] = None,
        min_minutes_left_entry: float = 2.0,
        min_edge_pp: float = 0.05,          # 5pp
        capture_frac: float = 0.70,         # take profit after capturing 70% of initial edge
        stop_frac: float = 0.50,            # stop after losing 50% of initial edge
        min_stop_pp: float = 0.06,          # 6pp floor for stop
        exit_minutes_left: float = 3.0,     # time-stop
        enable_edge_flip_exit: bool = True,
        edge_flip_pp: float = 0.02,         # 2pp: if holding is worse than selling now by >2pp
        dry_run: bool = False,
        subaccount: Optional[int] = None,
        run_id: Optional[str] = None,
        base_log_fields: Optional[Dict[str, Any]] = None,
        strict_log_schema: bool = False,
        full_config_on_start: Optional[Dict[str, Any]] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file

        self.subaccount = int(subaccount) if subaccount is not None else None
        self.log = TradeLogger(
            trade_log_file,
            run_id=run_id,
            base_fields=base_log_fields,
            strict_schema=bool(strict_log_schema),
        )

        self.fee_cents = int(fee_cents)
        self.count = int(count)
        self.max_contracts = max_contracts
        self.min_minutes_left_entry = float(min_minutes_left_entry)

        self.min_edge_pp = float(min_edge_pp)
        self.capture_frac = float(capture_frac)
        self.stop_frac = float(stop_frac)
        self.min_stop_pp = float(min_stop_pp)

        self.exit_minutes_left = float(exit_minutes_left)
        self.enable_edge_flip_exit = bool(enable_edge_flip_exit)
        self.edge_flip_pp = float(edge_flip_pp)

        self.dry_run = bool(dry_run)

        self._cap_logged = False

        start_payload: Dict[str, Any] = {
            "state_file": self.state_file,
            "trade_log_file": trade_log_file,
            "fee_cents": self.fee_cents,
            "count": self.count,
            "max_contracts": self.max_contracts,
            "min_edge_pp": self.min_edge_pp,
            "capture_frac": self.capture_frac,
            "stop_frac": self.stop_frac,
            "min_stop_pp": self.min_stop_pp,
            "exit_minutes_left": self.exit_minutes_left,
            "enable_edge_flip_exit": self.enable_edge_flip_exit,
            "edge_flip_pp": self.edge_flip_pp,
            "dry_run": self.dry_run,
        }
        if full_config_on_start:
            start_payload.update(full_config_on_start)
        self.log.log("bot_start", start_payload)

    # -------- cap helpers --------

    def _filled_contracts(self) -> int:
        return _filled_contracts_from_state(_read_state(self.state_file))

    def _remaining_contracts(self) -> Optional[int]:
        if self.max_contracts is None:
            return None
        return max(int(self.max_contracts) - self._filled_contracts(), 0)

    # -------- reconcile (copied/adapted from V0) --------

    def reconcile_state(self, event_ticker: str) -> None:
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
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return

        total_abs = 0
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

            total_abs += abs_pos
            if abs_pos > abs(top_position):
                top_position = pos_val
                top_ticker = mp.get("ticker") or mp.get("market_ticker")

        ts = _utc_ts()

        if total_abs <= 0 or not top_ticker:
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
            self.log.log("reconcile_cleared", {"event_ticker": event_ticker})
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
                "position_count": int(total_abs),
                "filled_contracts": int(total_abs),
                "fee_cents": int(self.fee_cents),
                "reconciled_at": ts,
                "reconciled": True,
            },
        )
        print(f"[TRADE] reconcile: {total_abs} contracts open for {event_ticker} (ticker={top_ticker}, side={side}).")
        self.log.log("reconcile_open", {
            "event_ticker": event_ticker,
            "market_ticker": top_ticker,
            "side": side,
            "position_count": int(total_abs),
        })

    def _event_minutes_left(self, event_ticker: str) -> Optional[float]:
        try:
            event_json = get_event(self.http, event_ticker)
            _, _, minutes_left = above_markets_from_event(event_json)
            return float(minutes_left)
        except Exception as e:
            print(f"[TRADE] expiry check failed for {event_ticker}: {e}")
            self.log.log("expiry_check_failed", {"event_ticker": event_ticker, "error": str(e)})
            return None

    def _close_state_expired(self, st: Dict[str, Any], *, reason: str, minutes_left: Optional[float]) -> None:
        entry_cost = st.get("entry_cost")
        snap: Dict[str, Any] = {
            "reason": reason,
            "event_ticker": st.get("event_ticker"),
            "market_ticker": st.get("market_ticker"),
            "side": st.get("side"),
            "position_count": _open_count_from_state(st),
            "entry_cost": float(entry_cost) if isinstance(entry_cost, (int, float)) else None,
            "buy_cents": st.get("buy_cents"),
            "strike": st.get("strike"),
            "entry_ts_utc": st.get("entry_ts_utc"),
            "minutes_left": float(minutes_left) if minutes_left is not None else None,
            "notes": ["expired_no_settlement_info"],
            "dry_run": self.dry_run,
        }
        self.log.log("position_expired", snap)

        _write_state(
            self.state_file,
            {
                "open": False,
                "event_ticker": st.get("event_ticker"),
                "filled_contracts": 0,
                "closed_reason": reason,
                "closed_ts_utc": _utc_ts(),
            },
        )
        print(f"[TRADE] position expired; state cleared -> {self.state_file}")

    # -------- entry selection --------

    def _pick_best_entry(self, result: EvaluationResult) -> Optional[EntryCandidate]:
        if result.minutes_left < self.min_minutes_left_entry:
            return None

        remaining = self._remaining_contracts()
        if remaining == 0:
            if not self._cap_logged:
                print(f"[TRADE] max contracts reached ({self.max_contracts}); trading disabled.")
                self._cap_logged = True
                self.log.log("cap_reached", {"max_contracts": self.max_contracts})
            return None

        best: Optional[EntryCandidate] = None

        for row in result.rows:
            if row.ob.ybuy is not None and row.ev_yes is not None:
                edge = float(row.ev_yes)
                if edge >= self.min_edge_pp:
                    c = EntryCandidate(
                        market_ticker=row.ticker,
                        side="yes",
                        buy_cents=int(row.ob.ybuy),
                        edge_pp=edge,
                        strike=float(row.strike),
                        p_model=float(row.p_model),
                        subtitle=str(row.subtitle),
                    )
                    if best is None or c.edge_pp > best.edge_pp:
                        best = c

            if row.ob.nbuy is not None and row.ev_no is not None:
                edge = float(row.ev_no)
                if edge >= self.min_edge_pp:
                    c = EntryCandidate(
                        market_ticker=row.ticker,
                        side="no",
                        buy_cents=int(row.ob.nbuy),
                        edge_pp=edge,
                        strike=float(row.strike),
                        p_model=float(row.p_model),
                        subtitle=str(row.subtitle),
                    )
                    if best is None or c.edge_pp > best.edge_pp:
                        best = c

        return best

    # -------- trading primitives --------

    def _place_order(
        self,
        *,
        reason: str,
        action: str,
        ticker: str,
        side: str,
        price_cents: int,
        count: int,
        reduce_only: bool,
        decision_fields: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": int(count),
            "type": "limit",
            "time_in_force": "fill_or_kill",
            "client_order_id": str(uuid.uuid4()),
        }
        if reduce_only:
            payload["reduce_only"] = True

        if side == "yes":
            payload["yes_price"] = int(price_cents)
        else:
            payload["no_price"] = int(price_cents)

        dec: Dict[str, Any] = {
            "action": "submit",
            "reason": str(reason),
            "market_ticker": str(ticker),
            "side": str(side),
            "source": "taker",
            "price_cents": int(price_cents),
            "count": int(count),
        }
        if decision_fields:
            dec.update(decision_fields)
        self.log.log("decision", dec)

        self.log.log(
            "order_submit",
            {
                "reason": str(reason),
                "action": action,
                "market_ticker": ticker,
                "ticker": ticker,  # legacy alias
                "side": side,
                "count": int(count),
                "price_cents": int(price_cents),
                "reduce_only": bool(reduce_only),
                "dry_run": self.dry_run,
            },
        )

        if self.dry_run:
            print(f"[TRADE] DRY_RUN {action.upper()} ...")
            return {"order": {"status": "filled", "fill_count": int(count)}}


        resp = create_order(self.http, self.auth, payload, base_url=self.kalshi_base_url, subaccount=self.subaccount)
        self.log.log("order_response", {
            "action": action,
            "ticker": ticker,
            "side": side,
            "count": int(count),
            "price_cents": int(price_cents),
            "reduce_only": bool(reduce_only),
            "resp": resp,
        })
        return resp

    def _compute_p_win_now(self, result: EvaluationResult, strike: float, side: str) -> float:
        p_yes = clamp01(lognormal_prob_above(result.market_state.spot, strike, result.market_state.sigma_blend, result.minutes_left))
        return p_yes if side == "yes" else (1.0 - p_yes)

    def _find_row(self, result: EvaluationResult, market_ticker: str):
        for row in result.rows:
            if row.ticker == market_ticker:
                return row
        return None

    # -------- PnL snapshot helpers --------

    def snapshot_pnl(self, last_result: Optional[EvaluationResult] = None) -> Dict[str, Any]:
        """
        Returns a best-effort snapshot of realized/unrealized PnL from state.
        If we can't compute (e.g. reconciled position without entry_cost), returns notes.
        """
        st = _read_state(self.state_file)
        out: Dict[str, Any] = {
            "open": bool(st.get("open")),
            "event_ticker": st.get("event_ticker"),
            "market_ticker": st.get("market_ticker"),
            "side": st.get("side"),
            "position_count": _open_count_from_state(st),
            "filled_contracts": _filled_contracts_from_state(st),
            "fee_cents": st.get("fee_cents", self.fee_cents),
            "notes": [],
        }

        entry_cost = st.get("entry_cost")
        if not isinstance(entry_cost, (int, float)):
            out["notes"].append("no_entry_cost_in_state (likely reconciled position); cannot compute PnL reliably")
            return out

        out["entry_cost"] = float(entry_cost)
        pos = _open_count_from_state(st)

        # mark-to-market if open
        if st.get("open") and pos > 0 and isinstance(st.get("market_ticker"), str) and st.get("side") in ("yes", "no"):
            try:
                ob_json = get_orderbook(self.http, st["market_ticker"])
                obs = parse_orderbook_stats(ob_json)
                bid_cents = obs.ybid if st["side"] == "yes" else obs.nbid
                if bid_cents is None:
                    out["notes"].append("no_bid_for_mtm")
                    return out
                net_exit = (int(bid_cents) - int(out["fee_cents"])) / 100.0
                pnl_per = net_exit - float(entry_cost)
                out["mtm_bid_cents"] = int(bid_cents)
                out["mtm_net_exit"] = float(net_exit)
                out["mtm_pnl_per_contract"] = float(pnl_per)
                out["mtm_pnl_total"] = float(pnl_per * pos)
            except Exception as e:
                out["notes"].append(f"mtm_failed: {e}")

        # realized info if present
        if isinstance(st.get("last_exit_bid_cents"), int):
            bid_cents = int(st["last_exit_bid_cents"])
            net_exit = (bid_cents - int(out["fee_cents"])) / 100.0
            pnl_per = net_exit - float(entry_cost)
            out["last_exit_bid_cents"] = bid_cents
            out["last_exit_net"] = float(net_exit)
            out["last_exit_pnl_per_contract"] = float(pnl_per)
            out["last_exit_reason"] = st.get("last_exit_reason")

        if last_result is not None:
            out["spot"] = float(last_result.market_state.spot)
            out["minutes_left"] = float(last_result.minutes_left)

        return out

    def on_shutdown(self, last_result: Optional[EvaluationResult] = None) -> None:
        snap = self.snapshot_pnl(last_result)
        self.log.log("bot_shutdown", snap)

    # -------- exit logic --------

    def _try_exit(self, result: EvaluationResult, st: Dict[str, Any]) -> None:
        ticker = st.get("market_ticker")
        side = st.get("side")

        if not isinstance(ticker, str) or side not in ("yes", "no"):
            return

        open_count = _open_count_from_state(st)
        if open_count <= 0:
            st["open"] = False
            _write_state(self.state_file, st)
            return

        try:
            ob_json = get_orderbook(self.http, ticker)
            obs = parse_orderbook_stats(ob_json)
        except Exception as e:
            print(f"[EXIT] orderbook fetch failed for {ticker}: {e}")
            self.log.log("exit_orderbook_failed", {"ticker": ticker, "side": side, "error": str(e)})
            return

        bid_cents = obs.ybid if side == "yes" else obs.nbid
        if bid_cents is None:
            print(f"[EXIT] no bid to exit {ticker} {side.upper()} (thin book).")
            self.log.log("exit_no_bid", {
                "ticker": ticker,
                "side": side,
                "minutes_left": float(result.minutes_left),
            })
            return

        net_exit_now = (int(bid_cents) - int(self.fee_cents)) / 100.0

        target_net_exit = st.get("target_net_exit")
        stop_net_exit = st.get("stop_net_exit")

        print(
            f"[EXITCHK] ticker={ticker} side={side.upper()} "
            f"bid={bid_cents}c net_exit_now=${net_exit_now:.4f} "
            f"target={target_net_exit} stop={stop_net_exit} "
            f"mins_left={result.minutes_left:.2f}"
            )

        reason: Optional[str] = None

        if isinstance(target_net_exit, (int, float)) and net_exit_now >= float(target_net_exit):
            reason = "take_profit_capture_edge"
        elif isinstance(stop_net_exit, (int, float)) and net_exit_now <= float(stop_net_exit):
            reason = "stop_loss_capture_edge"
        elif result.minutes_left <= self.exit_minutes_left:
            reason = "time_stop"
        elif self.enable_edge_flip_exit:
            row = self._find_row(result, ticker)
            if row is not None:
                p_win_now = self._compute_p_win_now(result, float(row.strike), side)
                hold_advantage = p_win_now - net_exit_now
                if hold_advantage <= -self.edge_flip_pp:
                    reason = f"edge_flip({hold_advantage:.3f})"

        if reason is None:
            return

        sell_count = min(int(self.count), int(open_count))
        print(
            f"[EXIT] {reason} ticker={ticker} side={side.upper()} bid={bid_cents}c net_exit_now=${net_exit_now:.4f} "
            f"mins_left={result.minutes_left:.2f} sell_count={sell_count}"
        )

        self.log.log("exit_signal", {
            "reason": reason,
            "market_ticker": ticker,
            "ticker": ticker,
            "side": side,
            "bid_cents": int(bid_cents),
            "net_exit_now": float(net_exit_now),
            "minutes_left": float(result.minutes_left),
            "sell_count": int(sell_count),
        })

        resp = self._place_order(
            reason="exit_signal",
            action="sell",
            ticker=ticker,
            side=side,
            price_cents=int(bid_cents),
            count=sell_count,
            reduce_only=True,
            decision_fields={
                "minutes_left": float(result.minutes_left),
                "spot": float(result.market_state.spot),
                "sigma_blend": float(result.market_state.sigma_blend),
            },
        )
        if resp is None:
            return

        if _is_filled(resp, sell_count):
            # update state
            new_open = open_count - sell_count
            st["position_count"] = int(max(new_open, 0))
            st["last_exit_reason"] = reason
            st["last_exit_bid_cents"] = int(bid_cents)
            st["last_exit_ts_utc"] = _utc_ts()
            if new_open <= 0:
                st["open"] = False
            _write_state(self.state_file, st)

            # log realized pnl for this exit
            entry_cost = st.get("entry_cost")
            pnl_per = None
            pnl_total = None
            if isinstance(entry_cost, (int, float)):
                net_exit = (int(bid_cents) - int(self.fee_cents)) / 100.0
                pnl_per = float(net_exit - float(entry_cost))
                pnl_total = float(pnl_per * sell_count)

            self.log.log("exit_filled", {
                "reason": reason,
                "market_ticker": ticker,
                "ticker": ticker,
                "side": side,
                "sell_count": int(sell_count),
                "exit_bid_cents": int(bid_cents),
                "exit_net": float((int(bid_cents) - int(self.fee_cents)) / 100.0),
                "entry_cost": float(entry_cost) if isinstance(entry_cost, (int, float)) else None,
                "pnl_per_contract": pnl_per,
                "pnl_total": pnl_total,
                "position_count_after": int(st.get("position_count", 0)),
                "open_after": bool(st.get("open")),
            })

            print(f"[EXIT] filled. state updated -> {self.state_file}")
        else:
            self.log.log("exit_not_filled", {"ticker": ticker, "side": side, "sell_count": int(sell_count)})
            print("[EXIT] not filled (FoK).")

    # -------- entry logic --------

    def _enter(self, result: EvaluationResult, cand: EntryCandidate) -> None:
        remaining = self._remaining_contracts()
        if remaining == 0:
            return

        order_count = int(self.count)
        if remaining is not None:
            order_count = min(order_count, int(remaining))
            if order_count <= 0:
                return

        p_win_entry = float(cand.p_model) if cand.side == "yes" else (1.0 - float(cand.p_model))
        entry_cost = (int(cand.buy_cents) + int(self.fee_cents)) / 100.0
        edge_pp = float(cand.edge_pp)

        target_pp = self.capture_frac * edge_pp
        stop_pp = max(self.min_stop_pp, self.stop_frac * edge_pp)

        target_net_exit = entry_cost + target_pp
        stop_net_exit = entry_cost - stop_pp

        print(
            f"[ENTRY] {cand.market_ticker} {cand.side.upper()} buy={cand.buy_cents}c "
            f"edge_pp={edge_pp:.3f} target_pp={target_pp:.3f} stop_pp={stop_pp:.3f} "
            f"mins_left={result.minutes_left:.2f} count={order_count}"
        )

        self.log.log("entry_signal", {
            "event_ticker": result.event_ticker,
            "market_ticker": cand.market_ticker,
            "side": cand.side,
            "buy_cents": int(cand.buy_cents),
            "count": int(order_count),
            "edge_pp": float(edge_pp),
            "p_win_entry": float(p_win_entry),
            "entry_cost": float(entry_cost),
            "target_net_exit": float(target_net_exit),
            "stop_net_exit": float(stop_net_exit),
            "minutes_left": float(result.minutes_left),
            "spot": float(result.market_state.spot),
        })

        resp = self._place_order(
            reason="entry_signal",
            action="buy",
            ticker=cand.market_ticker,
            side=cand.side,
            price_cents=int(cand.buy_cents),
            count=order_count,
            reduce_only=False,
            decision_fields={
                "event_ticker": str(result.event_ticker),
                "p_yes": float(cand.p_model),
                "p_win": float(p_win_entry),
                "edge_pp": float(edge_pp),
                "ev": float(edge_pp),
                "strike": float(cand.strike),
                "subtitle": str(cand.subtitle),
                "minutes_left": float(result.minutes_left),
                "spot": float(result.market_state.spot),
                "sigma_blend": float(result.market_state.sigma_blend),
            },
        )
        if resp is None:
            return

        if _is_filled(resp, order_count):
            st_prev = _read_state(self.state_file)
            prev_filled = _filled_contracts_from_state(st_prev)
            new_filled = prev_filled + int(order_count)

            state_out = {
                "open": True,
                "event_ticker": result.event_ticker,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "position_count": int(order_count),
                "filled_contracts": int(new_filled),
                "fee_cents": int(self.fee_cents),
                "buy_cents": int(cand.buy_cents),
                "strike": float(cand.strike),
                "p_win_entry": float(p_win_entry),
                "entry_cost": float(entry_cost),
                "edge_pp": float(edge_pp),
                "capture_frac": float(self.capture_frac),
                "stop_frac": float(self.stop_frac),
                "min_stop_pp": float(self.min_stop_pp),
                "target_net_exit": float(target_net_exit),
                "stop_net_exit": float(stop_net_exit),
                "entry_ts_utc": _utc_ts(),
            }
            _write_state(self.state_file, state_out)

            self.log.log("entry_filled", {
                "event_ticker": result.event_ticker,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(order_count),
                "buy_cents": int(cand.buy_cents),
                "entry_cost": float(entry_cost),
                "edge_pp": float(edge_pp),
                "target_net_exit": float(target_net_exit),
                "stop_net_exit": float(stop_net_exit),
                "filled_contracts_total": int(new_filled),
            })

            print(f"[ENTRY] filled. state written -> {self.state_file}")
        else:
            self.log.log("entry_not_filled", {
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(order_count),
                "buy_cents": int(cand.buy_cents),
            })
            print("[ENTRY] not filled (FoK).")

    # -------- main tick --------

    def on_tick(self, result: EvaluationResult) -> None:
        ms = result.market_state
        top_ev = None
        top_edge_pp = None
        top_market_ticker = None
        top_side = None
        for row in result.rows:
            if row.ob.ybuy is not None and row.ev_yes is not None:
                ev = float(row.ev_yes)
                if top_ev is None or ev > float(top_ev):
                    top_ev = ev
                    top_edge_pp = ev
                    top_market_ticker = str(row.ticker)
                    top_side = "yes"
            if row.ob.nbuy is not None and row.ev_no is not None:
                ev = float(row.ev_no)
                if top_ev is None or ev > float(top_ev):
                    top_ev = ev
                    top_edge_pp = ev
                    top_market_ticker = str(row.ticker)
                    top_side = "no"

        self.log.log(
            "tick_summary",
            {
                "event_ticker": str(result.event_ticker),
                "minutes_left": float(result.minutes_left),
                "spot": float(ms.spot),
                "sigma_implied": float(ms.sigma_implied),
                "sigma_realized": float(ms.sigma_realized),
                "sigma_blend": float(ms.sigma_blend),
                "confidence": str(ms.confidence),
                "num_rows_scanned": int(len(result.rows)),
                "top_ev": float(top_ev) if top_ev is not None else None,
                "top_edge_pp": float(top_edge_pp) if top_edge_pp is not None else None,
                "top_market_ticker": top_market_ticker,
                "top_side": top_side,
                "top_source": "taker" if top_market_ticker is not None else None,
            },
        )

        st = _read_state(self.state_file)

        if st.get("open"):
            st_event = st.get("event_ticker")
            if isinstance(st_event, str) and st_event != result.event_ticker:
                st_minutes_left = self._event_minutes_left(st_event)
                if st_minutes_left is not None and st_minutes_left <= 0:
                    self._close_state_expired(st, reason="event_expired", minutes_left=st_minutes_left)
                    return
            if result.minutes_left <= 0:
                self._close_state_expired(st, reason="event_expired", minutes_left=result.minutes_left)
                return
            self._try_exit(result, st)
            return

        cand = self._pick_best_entry(result)
        if cand is None:
            return

        self._enter(result, cand)
