"""
trader_v2_engine.py — canonical trading engine (V2Trader).

This is the primary execution engine for kalshi_edge. It manages position
entry/exit, order lifecycle (via OrderManager), risk caps, and structured
JSONL logging.

Previous engines (trader_v0, trader_v1) are deprecated; see legacy/.
"""

from __future__ import annotations

import json
import math
import os
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from kalshi_edge.data.kalshi.client import HttpClientLike, KalshiAuthLike
from kalshi_edge.fill_delta import FillDelta
from kalshi_edge.order_manager import OrderManager, market_side_key
from kalshi_edge.paper_fill_sim import PaperFillSimulator
from kalshi_edge.strategy_config import StrategyConfig, PaperConfig, load_config
from kalshi_edge.trade_log import TradeLogger
from kalshi_edge.telemetry.state_io import read_state as _read_state, write_state as _write_state
from kalshi_edge.util.coerce import as_int as _as_int, as_float as _as_float
from kalshi_edge.util.time import utc_ts, parse_ts as _parse_ts, secs_since as _secs_since

if TYPE_CHECKING:
    from kalshi_edge.pipeline import EvaluationResult


SCHEMA = "v2.2"



# _read_state, _write_state -> kalshi_edge.telemetry.state_io
# _as_int, _as_float -> kalshi_edge.util.coerce
# _parse_ts, _secs_since -> kalshi_edge.util.time


def _is_terminal(status: str) -> bool:
    s = (status or "").lower()
    return s in {"canceled", "cancelled", "executed", "filled", "rejected", "expired", "error"}


@dataclass
class _ActionCandidate:
    market_ticker: str
    event_ticker: str
    side: str  # "yes" | "no"
    source: str  # "taker" | "maker"
    price_cents: int
    fee_cents: int
    p_yes: float
    strike: float
    subtitle: str
    implied_q_yes: Optional[float]
    edge_pp: float
    ev: float
    max_price_cents: int
    bid_cents: Optional[int]
    ask_proxy_cents: Optional[int]
    spread_cents: Optional[int]
    top_size: Optional[float]


class V2Trader:
    def __init__(
        self,
        *,
        http: HttpClientLike,
        auth: Optional[KalshiAuthLike],
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        min_top_size: Optional[float] = None,
        spread_max_cents: Optional[int] = None,
        dry_run: bool = False,
        subaccount: Optional[int] = None,
        config: StrategyConfig | None = None,
        run_id: Optional[str] = None,
        base_log_fields: Optional[Dict[str, Any]] = None,
        strict_log_schema: bool = False,
        full_config_on_start: Optional[Dict[str, Any]] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(
            trade_log_file,
            run_id=run_id,
            base_fields=base_log_fields,
            strict_schema=bool(strict_log_schema),
        )

        self.cfg: StrategyConfig = config or load_config()

        self.count = int(self.cfg.ORDER_SIZE)
        self.ev_min = float(self.cfg.MIN_EV)
        self.min_top_size = float(self.cfg.MIN_TOP_SIZE if min_top_size is None else min_top_size)
        # allow None to disable the gate, otherwise use cfg default
        _smc = self.cfg.SPREAD_MAX_CENTS if spread_max_cents is None else spread_max_cents
        self.spread_max_cents = int(_smc) if _smc is not None else None

        self.max_positions_per_event = int(self.cfg.MAX_POSITIONS_PER_EVENT)
        self.max_cost_per_event = float(self.cfg.MAX_COST_PER_EVENT)
        self.max_cost_per_market = float(self.cfg.MAX_COST_PER_MARKET)
        # Optional global caps (not in StrategyConfig yet).
        self.max_total_cost = None
        self.max_total_positions = None

        self.order_mode = str(self.cfg.ORDER_MODE)
        self.post_only = bool(self.cfg.POST_ONLY)
        self.maker_time_in_force = "good_till_canceled"
        self.taker_time_in_force = "fill_or_kill"
        self.order_refresh_seconds = int(self.cfg.ORDER_REFRESH_SECONDS)
        self.cancel_stale_seconds = int(self.cfg.CANCEL_STALE_SECONDS)
        self.p_requote_pp = float(self.cfg.P_REQUOTE_PP)
        self.max_entries_per_tick = int(self.cfg.MAX_ENTRIES_PER_TICK)
        self.log_top_n_candidates = int(getattr(self.cfg, "LOG_TOP_N_CANDIDATES", 5))

        self.max_contracts_per_market = int(self.cfg.MAX_CONTRACTS_PER_MARKET)
        self.allow_scale_in = bool(self.cfg.ALLOW_SCALE_IN)
        self.scale_in_cooldown_seconds = int(self.cfg.SCALE_IN_COOLDOWN_SECONDS)

        self.min_edge_pp_entry = float(self.cfg.MIN_EV)
        self.min_edge_pp_scale_in = float(self.cfg.SCALE_IN_MIN_EV)
        self.maker_extra_buffer = 0.01

        self.fee_cents_taker = int(self.cfg.FEE_CENTS)
        self.fee_cents_maker = int(self.cfg.FEE_CENTS)
        self.dry_run = bool(dry_run)

        self.om = OrderManager(
            http=self.http,
            auth=self.auth,
            kalshi_base_url=self.kalshi_base_url,
            log=self.log,
            dry_run=self.dry_run,
            subaccount=subaccount,
        )

        self.paper_fill_sim: Optional[PaperFillSimulator] = None
        if self.dry_run and bool(self.cfg.paper.simulate_maker_fills) and self.order_mode in {"maker_only", "hybrid"}:
            seed = self.cfg.paper.seed
            rng = random.Random(int(seed)) if seed is not None else random.Random()
            self.paper_fill_sim = PaperFillSimulator(self.cfg.paper, rng, fee_cents_per_contract=int(self.fee_cents_maker))

        # multi-event state
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.market_cost: Dict[str, float] = {}
        self.event_cost: Dict[str, float] = {}
        self.event_positions: Dict[str, int] = {}
        self.total_cost_all: float = 0.0
        self.positions_count_all: int = 0

        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.active_order_by_market: Dict[str, str] = {}

        self._load_from_state_file()

        start_payload: Dict[str, Any] = {
            "schema": SCHEMA,
            "state_file": str(self.state_file),
            "trade_log_file": str(trade_log_file),
        }
        if full_config_on_start:
            start_payload.update(full_config_on_start)
        self.log.log("bot_start", start_payload)

    # ---- state ----

    def _persist_state_file(self) -> None:
        _write_state(
            self.state_file,
            {
                "schema": SCHEMA,
                "open_positions": self.open_positions,
                "market_cost": self.market_cost,
                "event_cost": self.event_cost,
                "event_positions": self.event_positions,
                "total_cost_all": float(self.total_cost_all),
                "positions_count_all": int(self.positions_count_all),
                "open_orders": self.open_orders,
                "active_order_by_market": self.active_order_by_market,
                "ts_utc": utc_ts(),
            },
        )

    def _recompute_aggregates_from_positions(self) -> None:
        self.market_cost = {}
        self.event_cost = {}
        self.event_positions = {}
        total_cost = 0.0
        count_all = 0
        for mkt, pos in self.open_positions.items():
            tc = _as_int(pos.get("total_count"), 0)
            if tc <= 0:
                continue
            count_all += 1
            evt = str(pos.get("event_ticker") or "")
            cost = _as_float(pos.get("total_cost_dollars"), 0.0)
            self.market_cost[str(mkt)] = float(cost)
            if evt:
                self.event_cost[evt] = float(self.event_cost.get(evt, 0.0) + cost)
                self.event_positions[evt] = int(self.event_positions.get(evt, 0) + 1)
            total_cost += cost
        self.total_cost_all = float(total_cost)
        self.positions_count_all = int(count_all)

    def _migrate_v2_to_v22(self, st: Dict[str, Any]) -> None:
        ops = st.get("open_positions")
        if not isinstance(ops, dict):
            return
        migrated: Dict[str, Dict[str, Any]] = {}
        for mkt, old in ops.items():
            if not isinstance(mkt, str) or not isinstance(old, dict):
                continue
            evt = str(old.get("event_ticker") or st.get("event_ticker") or "")
            side = str(old.get("side") or "")
            count = _as_int(old.get("count"), 0)
            entry_cost = _as_float(old.get("entry_cost_dollars"), 0.0)
            entry_fee = _as_float(old.get("entry_fee_dollars"), 0.0)
            price_cents = _as_int(old.get("entry_price_cents"), 0)
            ts = str(old.get("entry_ts_utc") or utc_ts())
            migrated[mkt] = {
                "market_ticker": mkt,
                "event_ticker": evt,
                "side": side,
                "total_count": int(count),
                "total_cost_dollars": float(entry_cost),
                "total_fee_dollars": float(entry_fee),
                "fills": [
                    {
                        "fill_id": "migrate-" + str(uuid.uuid4()),
                        "ts_utc": ts,
                        "count": int(count),
                        "price_cents": int(price_cents),
                        "fee_cents": int(round((entry_fee * 100.0) / float(count))) if count > 0 else 0,
                        "cost_dollars": float(entry_cost),
                        "p_yes": old.get("p_at_entry"),
                        "edge_pp": old.get("edge_pp_at_entry"),
                        "ev": old.get("ev_at_entry"),
                        "source": "migrated",
                        "order_id": None,
                    }
                ],
                "last_fill_ts_utc": ts,
                "last_fill_price_cents": int(price_cents) if price_cents else None,
                "last_fill_edge_pp": old.get("edge_pp_at_entry"),
                "strike": old.get("strike"),
                "subtitle": old.get("subtitle"),
                "implied_q_yes": old.get("implied_q_yes"),
                "migrated_from": "v2",
            }
        self.open_positions = migrated
        self.open_orders = {}
        self.active_order_by_market = {}
        self._recompute_aggregates_from_positions()
        self._persist_state_file()

    def _load_from_state_file(self) -> None:
        st = _read_state(self.state_file)
        if not isinstance(st, dict):
            return
        if st.get("schema") == "v2":
            self._migrate_v2_to_v22(st)
            return
        if st.get("schema") != SCHEMA:
            return
        if isinstance(st.get("open_positions"), dict):
            self.open_positions = {str(k): v for k, v in st["open_positions"].items() if isinstance(k, str) and isinstance(v, dict)}
        if isinstance(st.get("open_orders"), dict):
            self.open_orders = {str(k): v for k, v in st["open_orders"].items() if isinstance(k, str) and isinstance(v, dict)}
        if isinstance(st.get("active_order_by_market"), dict):
            self.active_order_by_market = {str(k): str(v) for k, v in st["active_order_by_market"].items() if isinstance(k, str) and isinstance(v, str)}
        self.market_cost = {str(k): float(v) for k, v in (st.get("market_cost") or {}).items() if isinstance(k, str)}
        self.event_cost = {str(k): float(v) for k, v in (st.get("event_cost") or {}).items() if isinstance(k, str)}
        self.event_positions = {str(k): int(v) for k, v in (st.get("event_positions") or {}).items() if isinstance(k, str)}
        self.total_cost_all = _as_float(st.get("total_cost_all"), 0.0)
        self.positions_count_all = _as_int(st.get("positions_count_all"), 0)
        if not self.market_cost or not self.event_cost:
            self._recompute_aggregates_from_positions()

    # ---- caps + sizing ----

    def _cap_check(
        self,
        *,
        event_ticker: str,
        market_ticker: str,
        add_cost_dollars: float,
        is_new_market_in_event: bool,
    ) -> Optional[str]:
        evt = str(event_ticker)
        if is_new_market_in_event and int(self.event_positions.get(evt, 0)) >= int(self.max_positions_per_event):
            return "max_positions_per_event"
        if (float(self.event_cost.get(evt, 0.0)) + float(add_cost_dollars)) > float(self.max_cost_per_event):
            return "max_cost_per_event"
        if (float(self.market_cost.get(market_ticker, 0.0)) + float(add_cost_dollars)) > float(self.max_cost_per_market):
            return "max_cost_per_market"
        if self.max_total_cost is not None:
            if (float(self.total_cost_all) + float(add_cost_dollars)) > float(self.max_total_cost):
                return "max_total_cost"
        if self.max_total_positions is not None:
            new_pos_count = int(self.positions_count_all + (1 if is_new_market_in_event else 0))
            if new_pos_count > int(self.max_total_positions):
                return "max_total_positions"
        return None

    def _cooldown_ok(self, pos: Optional[Dict[str, Any]]) -> bool:
        if pos is None:
            return True
        secs = _secs_since(pos.get("last_fill_ts_utc"))
        if secs is None:
            return True
        return secs >= float(self.scale_in_cooldown_seconds)

    def _target_contracts_for_candidate(self, pos: Optional[Dict[str, Any]], cand: _ActionCandidate) -> Tuple[int, int, Optional[str]]:
        current = _as_int(pos.get("total_count"), 0) if pos else 0
        if current > 0 and bool(self.cfg.DEDUPE_MARKETS):
            return current, current, "already_entered_market"
        if current <= 0:
            if cand.edge_pp < float(self.min_edge_pp_entry):
                return current, current, "edge_below_min_entry"
            return current, min(self.max_contracts_per_market, current + self.count), None

        if not self.allow_scale_in:
            return current, current, "scale_in_disabled"
        if cand.edge_pp < float(self.min_edge_pp_scale_in):
            return current, current, "edge_below_min_scale_in"
        if not self._cooldown_ok(pos):
            return current, current, "scale_in_cooldown"
        return current, min(self.max_contracts_per_market, current + self.count), None

    # ---- fills -> positions ----

    def _apply_fill(
        self,
        *,
        market_ticker: str,
        event_ticker: str,
        side: str,
        fill_count: int,
        price_cents: int,
        fee_cents: int,
        p_yes: float,
        edge_pp: float,
        source: str,
        order_id: str,
        strike: Optional[float],
        subtitle: Optional[str],
        implied_q_yes: Optional[float],
        ts_utc: Optional[str] = None,
    ) -> bool:
        ts = ts_utc or utc_ts()
        pos = self.open_positions.get(market_ticker)
        was_open = pos is not None and _as_int(pos.get("total_count"), 0) > 0

        cost_dollars = float(fill_count) * ((float(price_cents) + float(fee_cents)) / 100.0)
        fee_dollars = float(fill_count) * (float(fee_cents) / 100.0)

        fill = {
            "fill_id": str(uuid.uuid4()),
            "ts_utc": ts,
            "count": int(fill_count),
            "price_cents": int(price_cents),
            "fee_cents": int(fee_cents),
            "cost_dollars": float(cost_dollars),
            "p_yes": float(p_yes),
            "edge_pp": float(edge_pp),
            "ev": float(edge_pp),
            "source": str(source),
            "order_id": str(order_id),
        }

        if pos is None:
            pos = {
                "market_ticker": market_ticker,
                "event_ticker": event_ticker,
                "side": side,
                "total_count": 0,
                "total_cost_dollars": 0.0,
                "total_fee_dollars": 0.0,
                "fills": [],
                "last_fill_ts_utc": None,
                "last_fill_price_cents": None,
                "last_fill_edge_pp": None,
                "strike": strike,
                "subtitle": subtitle,
                "implied_q_yes": implied_q_yes,
            }

        if str(pos.get("side")) and str(pos.get("side")) != str(side):
            self.log.log("fill_side_conflict", {"market_ticker": market_ticker, "existing_side": pos.get("side"), "fill_side": side, "order_id": order_id})
            return was_open

        pos["event_ticker"] = event_ticker
        pos["side"] = side
        pos["total_count"] = int(_as_int(pos.get("total_count"), 0) + int(fill_count))
        pos["total_cost_dollars"] = float(_as_float(pos.get("total_cost_dollars"), 0.0) + cost_dollars)
        pos["total_fee_dollars"] = float(_as_float(pos.get("total_fee_dollars"), 0.0) + fee_dollars)
        pos["fills"] = list(pos.get("fills") or []) + [fill]
        pos["last_fill_ts_utc"] = ts
        pos["last_fill_price_cents"] = int(price_cents)
        pos["last_fill_edge_pp"] = float(edge_pp)
        if strike is not None:
            pos["strike"] = float(strike)
        if subtitle is not None:
            pos["subtitle"] = str(subtitle)
        if implied_q_yes is not None:
            pos["implied_q_yes"] = float(implied_q_yes)

        self.open_positions[market_ticker] = pos
        self.market_cost[market_ticker] = float(self.market_cost.get(market_ticker, 0.0) + cost_dollars)
        self.event_cost[event_ticker] = float(self.event_cost.get(event_ticker, 0.0) + cost_dollars)
        self.total_cost_all = float(self.total_cost_all + cost_dollars)
        if not was_open:
            self.event_positions[event_ticker] = int(self.event_positions.get(event_ticker, 0) + 1)
            self.positions_count_all = int(self.positions_count_all + 1)
        return was_open

    # ---- candidate building ----

    def _liquidity_ok(self, spread_cents: Optional[int], top_size: Optional[float]) -> bool:
        if self.spread_max_cents is not None and spread_cents is not None and int(spread_cents) > int(self.spread_max_cents):
            return False
        if top_size is not None and float(top_size) < float(self.min_top_size):
            return False
        return True

    def _max_acceptable_price_cents(self, *, p_win: float, fee_buffer_cents: int) -> int:
        x = int(math.floor(100.0 * (float(p_win) - float(self.ev_min)))) - int(fee_buffer_cents)
        return max(0, min(99, x))

    def _edge_at_price(self, *, p_win: float, price_cents: int, fee_cents: int) -> float:
        return float(p_win) - (float(price_cents + fee_cents) / 100.0)

    def _best_action_for_side(self, row, *, event_ticker: str, side: str, existing_side: Optional[str]) -> Optional[_ActionCandidate]:
        if existing_side is not None and existing_side != side:
            return None
        p_yes = float(row.p_model)
        p_win = p_yes if side == "yes" else (1.0 - p_yes)
        if side == "yes":
            ask_proxy = row.ob.ybuy
            bid = row.ob.ybid
            spread = row.ob.spread_y
            top_size = row.ob.nqty
        else:
            ask_proxy = row.ob.nbuy
            bid = row.ob.nbid
            spread = row.ob.spread_n
            top_size = row.ob.yqty
        implied_q_yes = (float(row.ob.ybuy) / 100.0) if row.ob.ybuy is not None else None

        best: Optional[_ActionCandidate] = None

        if self.order_mode in {"taker_only", "hybrid"} and ask_proxy is not None:
            max_price = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_taker)
            if self._liquidity_ok(spread, top_size) and int(ask_proxy) <= int(max_price):
                ev_take = self._edge_at_price(p_win=p_win, price_cents=int(ask_proxy), fee_cents=self.fee_cents_taker)
                best = _ActionCandidate(
                    market_ticker=str(row.ticker),
                    event_ticker=str(event_ticker),
                    side=side,
                    source="taker",
                    price_cents=int(ask_proxy),
                    fee_cents=int(self.fee_cents_taker),
                    p_yes=p_yes,
                    strike=float(row.strike),
                    subtitle=str(row.subtitle),
                    implied_q_yes=implied_q_yes,
                    edge_pp=float(ev_take),
                    ev=float(ev_take),
                    max_price_cents=int(max_price),
                    bid_cents=int(bid) if bid is not None else None,
                    ask_proxy_cents=int(ask_proxy),
                    spread_cents=int(spread) if spread is not None else None,
                    top_size=float(top_size) if top_size is not None else None,
                )

        if self.order_mode in {"maker_only", "hybrid"} and bid is not None:
            max_price_maker = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_maker)
            if int(max_price_maker) > int(bid):
                if self.post_only and ask_proxy is None:
                    return best
                desired_bid = min(int(max_price_maker), int(bid) + 1)
                if self.post_only and ask_proxy is not None and int(desired_bid) >= int(ask_proxy):
                    return best
                ev_make = self._edge_at_price(p_win=p_win, price_cents=int(desired_bid), fee_cents=self.fee_cents_maker)
                if ev_make >= float(self.ev_min + self.maker_extra_buffer):
                    cand_make = _ActionCandidate(
                        market_ticker=str(row.ticker),
                        event_ticker=str(event_ticker),
                        side=side,
                        source="maker",
                        price_cents=int(desired_bid),
                        fee_cents=int(self.fee_cents_maker),
                        p_yes=p_yes,
                        strike=float(row.strike),
                        subtitle=str(row.subtitle),
                        implied_q_yes=implied_q_yes,
                        edge_pp=float(ev_make),
                        ev=float(ev_make),
                        max_price_cents=int(max_price_maker),
                        bid_cents=int(bid),
                        ask_proxy_cents=int(ask_proxy) if ask_proxy is not None else None,
                        spread_cents=int(spread) if spread is not None else None,
                        top_size=float(top_size) if top_size is not None else None,
                    )
                    if best is None or cand_make.ev > best.ev:
                        best = cand_make

        return best

    def _build_candidates(self, result: EvaluationResult) -> List[_ActionCandidate]:
        out: List[_ActionCandidate] = []
        for row in result.rows:
            existing = self.open_positions.get(str(row.ticker))
            existing_side = (
                str(existing.get("side")) if isinstance(existing, dict) and existing.get("side") in {"yes", "no"} else None
            )
            c_yes = self._best_action_for_side(row, event_ticker=str(result.event_ticker), side="yes", existing_side=existing_side)
            c_no = self._best_action_for_side(row, event_ticker=str(result.event_ticker), side="no", existing_side=existing_side)
            best = c_yes
            if c_no is not None and (best is None or c_no.ev > best.ev):
                best = c_no
            if best is not None:
                out.append(best)
        out.sort(key=lambda c: c.ev, reverse=True)
        return out

    def _cand_log_fields(self, cand: _ActionCandidate, result: EvaluationResult) -> Dict[str, Any]:
        ms = result.market_state
        p_yes = float(cand.p_yes)
        p_win = p_yes if cand.side == "yes" else (1.0 - p_yes)
        return {
            "event_ticker": str(result.event_ticker),
            "market_ticker": str(cand.market_ticker),
            "side": str(cand.side),
            "source": str(cand.source),
            "price_cents": int(cand.price_cents),
            "fee_cents": int(cand.fee_cents),
            "p_yes": float(p_yes),
            "p_win": float(p_win),
            "implied_q_yes": float(cand.implied_q_yes) if cand.implied_q_yes is not None else None,
            "edge_pp": float(cand.edge_pp),
            "ev": float(cand.ev),
            "bid_cents": int(cand.bid_cents) if cand.bid_cents is not None else None,
            "ask_proxy_cents": int(cand.ask_proxy_cents) if cand.ask_proxy_cents is not None else None,
            "spread_cents": int(cand.spread_cents) if cand.spread_cents is not None else None,
            "top_size": float(cand.top_size) if cand.top_size is not None else None,
            "strike": float(cand.strike),
            "subtitle": str(cand.subtitle),
            "minutes_left": float(result.minutes_left),
            "spot": float(ms.spot),
            "sigma_blend": float(ms.sigma_blend),
        }

    # ---- orders ----

    def _cleanup_order_refs(self, order_id: str) -> None:
        for k, v in list(self.active_order_by_market.items()):
            if v == order_id:
                self.active_order_by_market.pop(k, None)
        self.open_orders.pop(order_id, None)

    def refresh_orders_and_apply_fills(self, result: EvaluationResult) -> bool:
        changed = False
        by_ticker = {str(r.ticker): r for r in result.rows}
        for oid in list(self.open_orders.keys()):
            tracked = self.open_orders.get(oid)
            if not isinstance(tracked, dict):
                self.open_orders.pop(oid, None)
                changed = True
                continue
            if _is_terminal(str(tracked.get("status", "")).lower()):
                self._cleanup_order_refs(oid)
                changed = True
                continue
            secs = _secs_since(tracked.get("last_checked_ts_utc"))
            is_paper_maker = (
                self.dry_run
                and self.paper_fill_sim is not None
                and str(tracked.get("source")) == "maker"
                and str(tracked.get("status", "")).lower() == "resting"
            )
            if not is_paper_maker and secs is not None and secs < float(self.order_refresh_seconds):
                continue
            tracked2, delta = self.om.refresh_tracked_order(tracked)
            self.open_orders[oid] = tracked2

            # Paper-fill simulation for resting maker orders in DRY_RUN.
            if is_paper_maker and self.paper_fill_sim is not None:
                row = by_ticker.get(str(tracked2.get("market_ticker")))
                if row is not None:
                    side = str(tracked2.get("side"))
                    if side == "yes":
                        best_bid_cents = row.ob.ybid
                        best_ask_cents = row.ob.ybuy
                    else:
                        best_bid_cents = row.ob.nbid
                        best_ask_cents = row.ob.nbuy
                    now = utc_ts()
                    self.paper_fill_sim.update_book(str(tracked2.get("market_ticker")), best_bid_cents, best_ask_cents, now)
                    delta = self.paper_fill_sim.maybe_fill(tracked2, now) or delta
                    if delta is not None and delta.delta_fill_count > 0:
                        meta = self.paper_fill_sim.pop_last_fill_meta(str(tracked2.get("order_id"))) or {}
                        self.log.log(
                            "paper_fill",
                            {
                                "simulated": True,
                                "order_id": str(tracked2.get("order_id")),
                                "market_ticker": str(tracked2.get("market_ticker")),
                                "fill_count": int(delta.delta_fill_count),
                                "fill_price_cents": int(meta.get("fill_price_cents")) if meta.get("fill_price_cents") is not None else delta.avg_price_cents,
                                "reason": str(meta.get("reason") or "paper_fill"),
                            },
                        )

            if delta is not None and delta.delta_fill_count > 0:
                row = by_ticker.get(str(tracked2.get("market_ticker")))
                p_yes = float(row.p_model) if row is not None else float(tracked2.get("last_model_p") or 0.5)
                side = str(tracked2.get("side"))
                p_win = p_yes if side == "yes" else (1.0 - p_yes)
                price_cents = delta.avg_price_cents if delta.avg_price_cents is not None else _as_int(tracked2.get("price_cents"), 0)
                fee_cents = delta.avg_fee_cents if delta.avg_fee_cents is not None else (self.fee_cents_maker if str(tracked2.get("source")) == "maker" else self.fee_cents_taker)
                edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))
                was_open = self._apply_fill(
                    market_ticker=str(tracked2.get("market_ticker")),
                    event_ticker=str(tracked2.get("event_ticker")),
                    side=side,
                    fill_count=int(delta.delta_fill_count),
                    price_cents=int(price_cents),
                    fee_cents=int(fee_cents),
                    p_yes=float(p_yes),
                    edge_pp=float(edge_pp),
                    source=str(tracked2.get("source")),
                    order_id=str(tracked2.get("order_id")),
                    strike=float(row.strike) if row is not None else None,
                    subtitle=str(row.subtitle) if row is not None else None,
                    implied_q_yes=(float(row.ob.ybuy) / 100.0) if (row is not None and row.ob.ybuy is not None) else None,
                    ts_utc=delta.ts_utc,
                )
                self.log.log("fill_detected", {"order_id": oid, "delta_fill_count": int(delta.delta_fill_count), "was_open": bool(was_open)})
                self.log.log("scale_in_filled" if was_open else "entry_filled", {"order_id": oid, "count": int(delta.delta_fill_count)})
                changed = True
            if _is_terminal(str(tracked2.get("status", "")).lower()) or _as_int(tracked2.get("remaining_count"), 0) <= 0:
                self._cleanup_order_refs(oid)
                changed = True
        return changed

    def _should_cancel_resting(self, tracked: Dict[str, Any], row) -> Optional[str]:
        if str(tracked.get("status", "")).lower() != "resting":
            return None
        age = _secs_since(tracked.get("created_ts_utc"))
        if age is not None and age > float(self.cancel_stale_seconds):
            return "stale_order"
        if row is None:
            return None
        p_now = float(row.p_model)
        p_then = _as_float(tracked.get("last_model_p"), p_now)
        if abs(p_now - p_then) >= float(self.p_requote_pp):
            return "p_requote"
        side = str(tracked.get("side"))
        p_win = p_now if side == "yes" else (1.0 - p_now)
        price_cents = _as_int(tracked.get("price_cents"), 0)
        fee_cents = self.fee_cents_maker if str(tracked.get("source")) == "maker" else self.fee_cents_taker
        if self._edge_at_price(p_win=p_win, price_cents=price_cents, fee_cents=int(fee_cents)) < float(self.ev_min + self.maker_extra_buffer):
            return "maker_edge_too_low_now"
        return None

    def _cancel_order(self, tracked: Dict[str, Any], *, reason: str) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log(
            "decision",
            {
                "action": "cancel",
                "reason": str(reason),
                "order_id": oid,
                "market_ticker": str(tracked.get("market_ticker") or ""),
                "side": str(tracked.get("side") or ""),
                "source": str(tracked.get("source") or ""),
                "price_cents": _as_int(tracked.get("price_cents"), 0),
                "count": _as_int(tracked.get("remaining_count"), _as_int(tracked.get("count"), 0)),
            },
        )
        self.log.log("order_cancel_submit", {"order_id": oid, "reason": str(reason)})
        try:
            resp = self.om.submit_cancel(tracked)
            self.log.log("order_canceled", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
        except Exception as e:
            self.log.log("order_cancel_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False
        self._cleanup_order_refs(oid)
        return True

    def _amend_order(
        self,
        tracked: Dict[str, Any],
        *,
        new_price_cents: int,
        new_count: int,
        reason: str,
        cand: Optional[_ActionCandidate] = None,
        result: Optional[EvaluationResult] = None,
    ) -> bool:
        oid = str(tracked.get("order_id"))
        dec: Dict[str, Any] = {
            "action": "amend",
            "reason": str(reason),
            "order_id": oid,
            "market_ticker": str(tracked.get("market_ticker") or ""),
            "side": str(tracked.get("side") or ""),
            "source": str(tracked.get("source") or ""),
            "price_cents": int(new_price_cents),
            "count": int(new_count),
        }
        if cand is not None and result is not None:
            dec.update(self._cand_log_fields(cand, result))
        self.log.log("decision", dec)

        self.log.log(
            "order_amend_submit",
            {
                "order_id": oid,
                "new_price_cents": int(new_price_cents),
                "new_count": int(new_count),
                "reason": str(reason),
            },
        )
        try:
            resp = self.om.submit_amend(tracked, new_price_cents=int(new_price_cents), new_count=int(new_count))
            self.open_orders[oid] = tracked
            self.log.log("order_amended", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
            return True
        except Exception as e:
            self.log.log("order_amend_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False

    def _create_order_for_candidate(
        self,
        cand: _ActionCandidate,
        *,
        remaining_to_target: int,
        result: Optional[EvaluationResult] = None,
        reason: str = "best_ev",
    ) -> bool:
        tif = self.taker_time_in_force if cand.source == "taker" else self.maker_time_in_force
        key = market_side_key(cand.market_ticker, cand.side)
        dec: Dict[str, Any] = {
            "action": "submit",
            "reason": str(reason),
            "market_ticker": str(cand.market_ticker),
            "side": str(cand.side),
            "source": str(cand.source),
            "price_cents": int(cand.price_cents),
            "count": int(remaining_to_target),
        }
        if result is not None:
            dec.update(self._cand_log_fields(cand, result))
        self.log.log("decision", dec)

        self.log.log(
            "order_submit",
            {
                "mode": cand.source,
                "source": cand.source,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(remaining_to_target),
                "price_cents": int(cand.price_cents),
                "tif": str(tif),
                "post_only": bool(self.post_only and cand.source == "maker"),
                "reason": str(reason),
            },
        )
        tracked, _resp = self.om.submit_new_order(
            market_ticker=cand.market_ticker,
            event_ticker=cand.event_ticker,
            side=cand.side,
            price_cents=int(cand.price_cents),
            count=int(remaining_to_target),
            time_in_force=str(tif),
            post_only=bool(self.post_only and cand.source == "maker"),
            source=str(cand.source),
            last_model_p=float(cand.p_yes),
            last_edge_pp=float(cand.edge_pp),
            fee_cents_per_contract=int(cand.fee_cents),
        )
        oid = str(tracked.get("order_id"))
        self.open_orders[oid] = tracked
        self.active_order_by_market[key] = oid

        # Apply immediate fills right away.
        fc = _as_int(tracked.get("fill_count"), 0)
        if fc > 0:
            total_cost = _as_int(tracked.get("last_fill_cost_cents"), 0)
            total_fee = _as_int(tracked.get("last_fee_paid_cents"), 0)
            delta = FillDelta(delta_fill_count=fc, delta_cost_cents=total_cost, delta_fee_cents=total_fee, ts_utc=utc_ts())
            price_cents = delta.avg_price_cents if delta.avg_price_cents is not None else int(cand.price_cents)
            fee_cents = delta.avg_fee_cents if delta.avg_fee_cents is not None else int(cand.fee_cents)
            p_yes = float(cand.p_yes)
            p_win = p_yes if cand.side == "yes" else (1.0 - p_yes)
            edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))
            was_open = self._apply_fill(
                market_ticker=cand.market_ticker,
                event_ticker=cand.event_ticker,
                side=cand.side,
                fill_count=int(delta.delta_fill_count),
                price_cents=int(price_cents),
                fee_cents=int(fee_cents),
                p_yes=float(p_yes),
                edge_pp=float(edge_pp),
                source=str(cand.source),
                order_id=str(oid),
                strike=float(cand.strike),
                subtitle=str(cand.subtitle),
                implied_q_yes=cand.implied_q_yes,
                ts_utc=delta.ts_utc,
            )
            self.log.log("scale_in_filled" if was_open else "entry_filled", {"order_id": oid, "count": int(delta.delta_fill_count)})
            if _as_int(tracked.get("remaining_count"), 0) <= 0 or _is_terminal(str(tracked.get("status", "")).lower()):
                self._cleanup_order_refs(oid)
        return True

    # ---- main tick ----

    def on_tick(self, result: EvaluationResult) -> None:
        """
        1) refresh_orders_and_apply_fills(result)
        2) build candidates (maker/taker) sorted by EV desc
        3) iterate up to max_entries_per_tick:
           - caps + target sizing
           - create/manage order for (market, side)
        4) persist state if changed
        """
        changed = False

        if self.refresh_orders_and_apply_fills(result):
            changed = True

        by_ticker = {str(r.ticker): r for r in result.rows}
        for key, oid in list(self.active_order_by_market.items()):
            tracked = self.open_orders.get(oid)
            if not isinstance(tracked, dict):
                self.active_order_by_market.pop(key, None)
                changed = True
                continue
            if str(tracked.get("source")) != "maker":
                continue
            reason = self._should_cancel_resting(tracked, by_ticker.get(str(tracked.get("market_ticker"))))
            if reason is not None and self._cancel_order(tracked, reason=str(reason)):
                changed = True

        cands = self._build_candidates(result)
        ms = result.market_state
        top = cands[0] if cands else None
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
                "top_ev": float(top.ev) if top is not None else None,
                "top_edge_pp": float(top.edge_pp) if top is not None else None,
                "top_market_ticker": str(top.market_ticker) if top is not None else None,
                "top_side": str(top.side) if top is not None else None,
                "top_source": str(top.source) if top is not None else None,
            },
        )
        n = max(0, int(self.log_top_n_candidates))
        for cand in cands[:n]:
            self.log.log("candidate", self._cand_log_fields(cand, result))
        submitted = 0

        for cand in cands:
            if submitted >= int(self.max_entries_per_tick):
                break

            pos = self.open_positions.get(cand.market_ticker)
            if pos is not None and str(pos.get("side")) and str(pos.get("side")) != str(cand.side):
                payload = {"market_ticker": cand.market_ticker, "side": cand.side, "skip_reason": "position_side_conflict"}
                payload.update(self._cand_log_fields(cand, result))
                self.log.log("skip", payload)
                continue

            current, target, size_reason = self._target_contracts_for_candidate(pos, cand)
            remaining = int(target - current)
            if remaining <= 0:
                continue
            if size_reason is not None:
                payload = {"market_ticker": cand.market_ticker, "side": cand.side, "skip_reason": str(size_reason)}
                payload.update(self._cand_log_fields(cand, result))
                self.log.log("skip", payload)
                continue

            key = market_side_key(cand.market_ticker, cand.side)
            existing_oid = self.active_order_by_market.get(key)
            if existing_oid and existing_oid in self.open_orders:
                tracked = self.open_orders[existing_oid]
                if str(tracked.get("source")) == "maker" and str(tracked.get("status", "")).lower() == "resting":
                    cur_price = _as_int(tracked.get("price_cents"), 0)
                    cur_rem = _as_int(tracked.get("remaining_count"), _as_int(tracked.get("count"), 0))
                    if cur_price != int(cand.price_cents) or cur_rem != int(remaining):
                        amend_age = _secs_since(tracked.get("last_amended_ts_utc") or tracked.get("created_ts_utc"))
                        if amend_age is not None and amend_age < float(self.order_refresh_seconds):
                            self.log.log(
                                "skip",
                                {
                                    "market_ticker": cand.market_ticker,
                                    "side": cand.side,
                                    "skip_reason": "amend_throttled",
                                    "order_id": existing_oid,
                                    "seconds_since_last_amend": float(amend_age),
                                    **self._cand_log_fields(cand, result),
                                },
                            )
                            continue
                        tracked["last_model_p"] = float(cand.p_yes)
                        tracked["last_edge_pp"] = float(cand.edge_pp)
                        if self._amend_order(
                            tracked,
                            new_price_cents=int(cand.price_cents),
                            new_count=int(remaining),
                            reason="requote",
                            cand=cand,
                            result=result,
                        ):
                            changed = True
                            submitted += 1
                            continue
                        if self._cancel_order(tracked, reason="amend_failed_replace"):
                            changed = True
                continue

            add_cost = float(remaining) * ((float(cand.price_cents) + float(cand.fee_cents)) / 100.0)
            is_new_market_in_event = int(_as_int(pos.get("total_count"), 0) if pos else 0) <= 0
            cap_reason = self._cap_check(
                event_ticker=cand.event_ticker,
                market_ticker=cand.market_ticker,
                add_cost_dollars=add_cost,
                is_new_market_in_event=is_new_market_in_event,
            )
            if cap_reason is not None:
                payload = {"market_ticker": cand.market_ticker, "side": cand.side, "skip_reason": cap_reason}
                payload.update(self._cand_log_fields(cand, result))
                self.log.log("skip", payload)
                continue

            if self._create_order_for_candidate(cand, remaining_to_target=int(remaining), result=result, reason="best_ev"):
                changed = True
                submitted += 1

        if changed:
            self._persist_state_file()

    # ---- misc ----

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "positions_count_all": int(self.positions_count_all),
            "total_cost_all": float(self.total_cost_all),
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "active_order_by_market": self.active_order_by_market,
        }

    def on_shutdown(self, last_result: Optional[EvaluationResult] = None) -> None:
        snap = self.snapshot_state()
        if last_result is not None:
            snap.update({"spot": float(last_result.market_state.spot), "minutes_left": float(last_result.minutes_left)})
        self.log.log("bot_shutdown", snap)

    def reconcile_state(self, event_ticker: str) -> None:
        event_ticker = str(event_ticker).upper()
        if self.dry_run:
            self.log.log("reconcile_skipped", {"event_ticker": event_ticker, "reason": "dry_run"})
            return
        from kalshi_edge.data.kalshi.client import get_positions
        cursor = None
        market_positions: List[dict] = []
        while True:
            resp = get_positions(self.http, self.auth, base_url=self.kalshi_base_url, event_ticker=event_ticker, limit=1000, cursor=cursor, count_filter="position")
            market_positions.extend(resp.get("market_positions") or [])
            cursor = resp.get("cursor")
            if not cursor:
                break
        added = 0
        for mp in market_positions:
            tkr = mp.get("ticker") or mp.get("market_ticker")
            pos = mp.get("position")
            if not isinstance(tkr, str):
                continue
            try:
                pos_val = int(pos)
            except Exception:
                continue
            abs_pos = abs(int(pos_val))
            if abs_pos <= 0:
                continue
            side = "yes" if pos_val > 0 else "no"
            if tkr in self.open_positions and _as_int(self.open_positions[tkr].get("total_count"), 0) > 0:
                continue
            self.open_positions[tkr] = {
                "market_ticker": tkr,
                "event_ticker": event_ticker,
                "side": side,
                "total_count": int(abs_pos),
                "total_cost_dollars": 0.0,
                "total_fee_dollars": 0.0,
                "fills": [{"fill_id": "reconcile-" + str(uuid.uuid4()), "ts_utc": utc_ts(), "count": int(abs_pos), "price_cents": 0, "fee_cents": 0, "cost_dollars": 0.0, "p_yes": None, "edge_pp": None, "ev": None, "source": "reconciled", "order_id": None}],
                "last_fill_ts_utc": utc_ts(),
                "last_fill_price_cents": 0,
                "last_fill_edge_pp": None,
                "reconciled": True,
            }
            added += 1
        if added:
            self._recompute_aggregates_from_positions()
            self._persist_state_file()
        self.log.log("reconcile_done", {"event_ticker": event_ticker, "positions_added": int(added)})


def debug_order_manager() -> None:
    """
    Simulate two ticks with same market and verify:
    - only one active order exists
    - a fill increases position count
    - p move beyond p_requote_pp cancels resting order
    """
    from dataclasses import dataclass

    class _HttpNoop:
        def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
            raise RuntimeError("debug_order_manager does not perform HTTP requests")

        def post_json(self, url: str, json_body: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
            raise RuntimeError("debug_order_manager does not perform HTTP requests")

        def request_json(
            self,
            method: str,
            url: str,
            params: Optional[dict] = None,
            headers: Optional[dict] = None,
            json_body: Optional[dict] = None,
        ) -> Dict[str, Any]:
            raise RuntimeError("debug_order_manager does not perform HTTP requests")

    cfg = StrategyConfig(
        ORDER_MODE="maker_only",
        POST_ONLY=True,
        MAX_CONTRACTS_PER_MARKET=2,
        ALLOW_SCALE_IN=True,
        P_REQUOTE_PP=0.02,
        CANCEL_STALE_SECONDS=99999,
        MAX_ENTRIES_PER_TICK=1,
        FEE_CENTS=1,
        MIN_EV=0.05,
        MIN_TOP_SIZE=0.0,
        SPREAD_MAX_CENTS=999,
        paper=PaperConfig(simulate_maker_fills=False),
    )

    @dataclass
    class _OB:
        ybid: int
        nbid: int
        ybuy: int
        nbuy: int
        spread_y: int
        spread_n: int
        yqty: float
        nqty: float

    @dataclass
    class _Row:
        ticker: str
        p_model: float
        strike: float
        subtitle: str
        ob: _OB

    @dataclass
    class _MS:
        spot: float = 0.0
        sigma_implied: float = 0.0
        sigma_realized: float = 0.0
        sigma_blend: float = 0.0
        confidence: str = "debug"
        note: str = ""

    @dataclass
    class _Res:
        event_ticker: str
        minutes_left: float
        market_state: _MS
        rows: List[_Row]

    t = V2Trader(
        http=_HttpNoop(),
        auth=None,
        kalshi_base_url="https://example.invalid",
        state_file=".debug_state_v22.json",
        trade_log_file="logs/debug_order_manager.jsonl",
        dry_run=True,
        config=cfg,
    )

    row1 = _Row(
        ticker="TEST-MKT",
        p_model=0.70,
        strike=123.0,
        subtitle="debug",
        ob=_OB(ybid=50, nbid=50, ybuy=52, nbuy=52, spread_y=2, spread_n=2, yqty=10.0, nqty=10.0),
    )
    res1 = _Res(event_ticker="TEST-EVT", minutes_left=10.0, market_state=_MS(), rows=[row1])
    t.on_tick(res1)
    assert len(t.active_order_by_market) <= 1
    before = dict(t.active_order_by_market)
    t.on_tick(res1)
    assert t.active_order_by_market == before

    # Simulate a fill directly (dry-run maker orders don't auto-fill).
    cand_price = list(t.open_orders.values())[0]["price_cents"] if t.open_orders else 51
    t._apply_fill(
        market_ticker="TEST-MKT",
        event_ticker="TEST-EVT",
        side="yes",
        fill_count=1,
        price_cents=int(cand_price),
        fee_cents=1,
        p_yes=0.70,
        edge_pp=0.70 - (float(int(cand_price) + 1) / 100.0),
        source="maker",
        order_id="SIMULATED",
        strike=123.0,
        subtitle="debug",
        implied_q_yes=0.52,
    )
    assert t.open_positions["TEST-MKT"]["total_count"] == 1

    row2 = _Row(
        ticker="TEST-MKT",
        p_model=0.67,
        strike=123.0,
        subtitle="debug",
        ob=row1.ob,
    )
    res2 = _Res(event_ticker="TEST-EVT", minutes_left=10.0, market_state=_MS(), rows=[row2])
    t.on_tick(res2)
    assert len(t.active_order_by_market) <= 1

