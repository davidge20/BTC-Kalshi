"""
trader_v2.py

Public entrypoint for the v2 trader.

The implementation lives in `kalshi_edge.trader_v2_engine` and is configured via
`kalshi_edge.strategy_config` (JSON via env var `KALSHI_EDGE_CONFIG_JSON`).
"""

# from __future__ import annotations

from kalshi_edge.strategy_config import StrategyConfig, load_config
from kalshi_edge.trader_v2_engine import V2Trader, debug_order_manager

__all__ = ["V2Trader", "debug_order_manager", "StrategyConfig", "load_config"]

"""
trader_v2.py

Public entrypoint for the v2 trader.

The implementation lives in `kalshi_edge.trader_v2_engine` and is configured via
`kalshi_edge.strategy_config` (JSON via env var `KALSHI_EDGE_CONFIG_JSON`).
"""

# from __future__ import annotations

from kalshi_edge.strategy_config import StrategyConfig, load_config
from kalshi_edge.trader_v2_engine import V2Trader, debug_order_manager

__all__ = ["V2Trader", "debug_order_manager", "StrategyConfig", "load_config"]

"""
trader_v2.py

Public entrypoint for the v2 trader.

Implementation lives in `kalshi_edge.trader_v2_engine` and is configured via
`kalshi_edge.strategy_config` (JSON via env var `KALSHI_EDGE_CONFIG_JSON`).
"""

# from __future__ import annotations

from kalshi_edge.strategy_config import StrategyConfig, load_config
from kalshi_edge.trader_v2_engine import V2Trader, debug_order_manager

__all__ = ["V2Trader", "debug_order_manager", "StrategyConfig", "load_config"]

"""
trader_v2.py

V2 trader: hold-to-expiration entry engine with optional strategy config.

Strategy parameters are loaded from a single JSON config file when the env var
`KALSHI_EDGE_CONFIG_JSON` is set. When unset, defaults match prior behavior.
"""

# from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.order_manager import FillDelta, OrderManager, market_side_key, utc_ts
from kalshi_edge.pipeline import EvaluationResult
from kalshi_edge.strategy_config import StrategyConfig, load_config
from kalshi_edge.trade_log import TradeLogger


SCHEMA = "v2.2"


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


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _secs_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


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
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        dry_run: bool = False,
        subaccount: Optional[int] = None,
        config: StrategyConfig | None = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(trade_log_file)
        self.dry_run = bool(dry_run)

        self.cfg: StrategyConfig = config or load_config()

        # core knobs from config
        self.count = int(self.cfg.ORDER_SIZE)
        self.ev_min = float(self.cfg.MIN_EV)
        self.max_positions_per_event = int(self.cfg.MAX_POSITIONS_PER_EVENT)
        self.max_cost_per_event = float(self.cfg.MAX_COST_PER_EVENT)
        self.max_cost_per_market = float(self.cfg.MAX_COST_PER_MARKET)
        self.max_entries_per_tick = int(self.cfg.MAX_ENTRIES_PER_TICK)
        self.max_contracts_per_market = int(self.cfg.MAX_CONTRACTS_PER_MARKET)

        self.allow_scale_in = bool(self.cfg.ALLOW_SCALE_IN)
        self.scale_in_cooldown_seconds = int(self.cfg.SCALE_IN_COOLDOWN_SECONDS)
        self.min_edge_pp_entry = float(self.cfg.MIN_EV)
        self.min_edge_pp_scale_in = float(self.cfg.SCALE_IN_MIN_EV)

        # execution/maker placeholders from config
        self.order_mode = str(self.cfg.ORDER_MODE)
        self.post_only = bool(self.cfg.POST_ONLY)
        self.order_refresh_seconds = int(self.cfg.ORDER_REFRESH_SECONDS)
        self.cancel_stale_seconds = int(self.cfg.CANCEL_STALE_SECONDS)
        self.p_requote_pp = float(self.cfg.P_REQUOTE_PP)

        self.fee_cents_taker = int(self.cfg.FEE_CENTS)
        self.fee_cents_maker = int(self.cfg.FEE_CENTS)
        self.maker_extra_buffer = 0.01  # unchanged default

        self.om = OrderManager(
            http=self.http,
            auth=self.auth,
            kalshi_base_url=self.kalshi_base_url,
            log=self.log,
            dry_run=self.dry_run,
            subaccount=subaccount,
        )

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

        self.log.log(
            "bot_start",
            {
                "schema": SCHEMA,
                "config": {k: getattr(self.cfg, k) for k in self.cfg.__dataclass_fields__.keys()},
                "dry_run": self.dry_run,
            },
        )

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
            # preserve old "already_entered_market" semantics
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
            if int(ask_proxy) <= int(max_price):
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

        # Maker logic present but not forced; config contains placeholders.
        if self.order_mode in {"maker_only", "hybrid"} and bid is not None:
            max_price_maker = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_maker)
            if int(max_price_maker) > int(bid):
                if self.post_only and ask_proxy is not None and int(bid + 1) >= int(ask_proxy):
                    return best
                desired_bid = min(int(max_price_maker), int(bid) + 1)
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
            if secs is not None and secs < float(self.order_refresh_seconds):
                continue
            tracked2, delta = self.om.refresh_tracked_order(tracked)
            self.open_orders[oid] = tracked2
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
        self.log.log("order_cancel_submit", {"order_id": oid, "reason": str(reason)})
        try:
            resp = self.om.submit_cancel(tracked)
            self.log.log("order_canceled", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
        except Exception as e:
            self.log.log("order_cancel_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False
        self._cleanup_order_refs(oid)
        return True

    def _amend_order(self, tracked: Dict[str, Any], *, new_price_cents: int, new_count: int) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log("order_amend_submit", {"order_id": oid, "new_price_cents": int(new_price_cents), "new_count": int(new_count)})
        try:
            resp = self.om.submit_amend(tracked, new_price_cents=int(new_price_cents), new_count=int(new_count))
            self.open_orders[oid] = tracked
            self.log.log("order_amended", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
            return True
        except Exception as e:
            self.log.log("order_amend_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False

    def _create_order_for_candidate(self, cand: _ActionCandidate, *, remaining_to_target: int) -> bool:
        tif = "fill_or_kill" if cand.source == "taker" else "good_till_canceled"
        key = market_side_key(cand.market_ticker, cand.side)
        self.log.log("order_submit", {"mode": cand.source, "market_ticker": cand.market_ticker, "side": cand.side, "count": int(remaining_to_target), "price_cents": int(cand.price_cents), "tif": str(tif)})
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
            self._apply_fill(
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
            if _as_int(tracked.get("remaining_count"), 0) <= 0 or _is_terminal(str(tracked.get("status", "")).lower()):
                self._cleanup_order_refs(oid)
        return True

    # ---- main tick ----

    def on_tick(self, result: EvaluationResult) -> None:
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
        submitted = 0

        for cand in cands:
            if submitted >= int(self.max_entries_per_tick):
                break

            pos = self.open_positions.get(cand.market_ticker)
            if pos is not None and str(pos.get("side")) and str(pos.get("side")) != str(cand.side):
                continue

            current, target, size_reason = self._target_contracts_for_candidate(pos, cand)
            remaining = int(target - current)
            if remaining <= 0:
                continue
            if size_reason is not None:
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
                            continue
                        tracked["last_model_p"] = float(cand.p_yes)
                        tracked["last_edge_pp"] = float(cand.edge_pp)
                        if self._amend_order(tracked, new_price_cents=int(cand.price_cents), new_count=int(remaining)):
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
                continue

            if self._create_order_for_candidate(cand, remaining_to_target=int(remaining)):
                changed = True
                submitted += 1

        if changed:
            self._persist_state_file()

        print(
            f"[TRADE] tick event={result.event_ticker} candidates={len(cands)} entries={submitted} "
            f"total_cost_all=${self.total_cost_all:.4f} positions_all={self.positions_count_all} open_orders={len(self.open_orders)}"
        )

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
        from kalshi_edge.kalshi_api import get_positions
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
    Minimal sanity mode: two ticks, verify no duplicates and scaling semantics.
    """
    from dataclasses import dataclass

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
        sigma_blend: float = 0.0

    cfg = StrategyConfig(
        MIN_EV=0.05,
        ORDER_SIZE=1,
        MAX_COST_PER_EVENT=5.0,
        MAX_POSITIONS_PER_EVENT=10,
        MAX_COST_PER_MARKET=1.0,
        MAX_CONTRACTS_PER_MARKET=2,
        DEDUPE_MARKETS=False,
        ALLOW_SCALE_IN=True,
        SCALE_IN_COOLDOWN_SECONDS=120,
        SCALE_IN_MIN_EV=0.06,
        MAX_ENTRIES_PER_TICK=1,
        FEE_CENTS=1,
        ORDER_MODE="maker_only",
        POST_ONLY=True,
        ORDER_REFRESH_SECONDS=10,
        CANCEL_STALE_SECONDS=60,
        P_REQUOTE_PP=0.02,
    )
    cfg.validate()

    t = V2Trader(
        http=HttpClient(debug=False),
        auth=KalshiAuth(api_key_id="DUMMY", private_key_path="/dev/null"),
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
    res1 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row1])  # type: ignore[arg-type]
    t.on_tick(res1)
    assert len(t.active_order_by_market) <= 1
    before = dict(t.active_order_by_market)
    t.on_tick(res1)
    assert t.active_order_by_market == before

"""
trader_v2.py

Compatibility wrapper for the clean v2.2 strategy engine.

The implementation lives in `kalshi_edge.trader_v2_engine`.
"""

# from __future__ import annotations

from kalshi_edge.trader_v2_engine import V2Trader, debug_order_manager

__all__ = ["V2Trader", "debug_order_manager"]

"""
trader_v2.py

V2.2 trader: hold-to-expiration entry engine with resting-order support.

Key properties:
- Entry only (BUY): never submits exits.
- Multi-event state: can trade across discovered events (optional lock in run.py).
- Strategy iteration engine: maintains at most ONE active order per (market_ticker, side).
- Hybrid maker/taker: can place post-only resting orders when taker price isn't good enough.
- Scale-in is explicit via target sizing + caps; no duplicate order spam in watch mode.
"""

# from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.strategy_config import DEFAULT_CONFIG as _DEFAULT_CFG

EV_MIN = _DEFAULT_CFG.MIN_EV
ORDER_SIZE = _DEFAULT_CFG.ORDER_SIZE
MIN_TOP_SIZE = _DEFAULT_CFG.MIN_TOP_SIZE
SPREAD_MAX_CENTS = _DEFAULT_CFG.SPREAD_MAX_CENTS
MAX_POSITIONS_PER_EVENT = _DEFAULT_CFG.MAX_POSITIONS_PER_EVENT
MAX_COST_PER_EVENT = _DEFAULT_CFG.MAX_COST_PER_EVENT
MAX_COST_PER_STRIKE = _DEFAULT_CFG.MAX_COST_PER_MARKET
from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.order_manager import FillDelta, OrderManager, market_side_key, utc_ts
from kalshi_edge.pipeline import EvaluationResult
from kalshi_edge.trade_log import TradeLogger


SCHEMA = "v2.2"


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


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _secs_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


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
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        count: int = ORDER_SIZE,
        ev_min: float = EV_MIN,
        min_top_size: float = MIN_TOP_SIZE,
        spread_max_cents: Optional[int] = SPREAD_MAX_CENTS,
        max_positions_per_event: int = MAX_POSITIONS_PER_EVENT,
        max_cost_per_event: float = MAX_COST_PER_EVENT,
        dry_run: bool = False,
        max_cost_per_market: Optional[float] = None,
        max_total_cost: Optional[float] = None,
        max_total_positions: Optional[int] = None,
        order_mode: str = "hybrid",  # taker_only | maker_only | hybrid
        post_only: bool = True,
        maker_time_in_force: str = "good_till_canceled",
        taker_time_in_force: str = "fill_or_kill",
        order_refresh_seconds: int = 10,
        cancel_stale_seconds: int = 60,
        p_requote_pp: float = 0.02,
        max_entries_per_tick: int = 1,
        max_contracts_per_market: int = 1,
        allow_scale_in: Optional[bool] = None,
        scale_in_cooldown_seconds: int = 60,
        min_edge_pp_entry: Optional[float] = None,
        min_edge_pp_scale_in: Optional[float] = None,
        maker_extra_buffer: float = 0.01,
        fee_cents_taker: int = 1,
        fee_cents_maker: int = 1,
        subaccount: Optional[int] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(trade_log_file)

        self.count = int(count)
        self.ev_min = float(ev_min)
        self.min_top_size = float(min_top_size)
        self.spread_max_cents = int(spread_max_cents) if spread_max_cents is not None else None
        self.max_positions_per_event = int(max_positions_per_event)
        self.max_cost_per_event = float(max_cost_per_event)
        self.max_cost_per_market = float(max_cost_per_market) if max_cost_per_market is not None else None
        self.max_total_cost = float(max_total_cost) if max_total_cost is not None else None
        self.max_total_positions = int(max_total_positions) if max_total_positions is not None else None

        self.order_mode = str(order_mode)
        self.post_only = bool(post_only)
        self.maker_time_in_force = str(maker_time_in_force)
        self.taker_time_in_force = str(taker_time_in_force)
        self.order_refresh_seconds = int(order_refresh_seconds)
        self.cancel_stale_seconds = int(cancel_stale_seconds)
        self.p_requote_pp = float(p_requote_pp)
        self.max_entries_per_tick = int(max_entries_per_tick)
        self.max_contracts_per_market = int(max_contracts_per_market)
        if allow_scale_in is None:
            allow_scale_in = self.max_contracts_per_market > 1
        self.allow_scale_in = bool(allow_scale_in)
        self.scale_in_cooldown_seconds = int(scale_in_cooldown_seconds)
        self.min_edge_pp_entry = float(min_edge_pp_entry) if min_edge_pp_entry is not None else float(self.ev_min)
        self.min_edge_pp_scale_in = (
            float(min_edge_pp_scale_in) if min_edge_pp_scale_in is not None else float(self.ev_min + 0.01)
        )
        self.maker_extra_buffer = float(maker_extra_buffer)
        self.fee_cents_taker = int(fee_cents_taker)
        self.fee_cents_maker = int(fee_cents_maker)
        self.dry_run = bool(dry_run)

        self.om = OrderManager(
            http=self.http,
            auth=self.auth,
            kalshi_base_url=self.kalshi_base_url,
            log=self.log,
            dry_run=self.dry_run,
            subaccount=subaccount,
        )

        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.market_cost: Dict[str, float] = {}
        self.event_cost: Dict[str, float] = {}
        self.event_positions: Dict[str, int] = {}
        self.total_cost_all: float = 0.0
        self.positions_count_all: int = 0

        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.active_order_by_market: Dict[str, str] = {}

        self._load_from_state_file()

        self.log.log(
            "bot_start",
            {
                "schema": SCHEMA,
                "state_file": self.state_file,
                "count": self.count,
                "ev_min": self.ev_min,
                "order_mode": self.order_mode,
                "post_only": self.post_only,
                "max_contracts_per_market": self.max_contracts_per_market,
                "fee_cents_taker": self.fee_cents_taker,
                "fee_cents_maker": self.fee_cents_maker,
                "dry_run": self.dry_run,
            },
        )

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

        for mkt, pos in self.open_positions.items():
            tc = _as_int(pos.get("total_count"), 0)
            if tc <= 0:
                continue
            evt = str(pos.get("event_ticker") or "")
            cost = _as_float(pos.get("total_cost_dollars"), 0.0)
            self.market_cost[str(mkt)] = float(cost)
            if evt:
                self.event_cost[evt] = float(self.event_cost.get(evt, 0.0) + cost)
                self.event_positions[evt] = int(self.event_positions.get(evt, 0) + 1)
            total_cost += cost

        self.total_cost_all = float(total_cost)
        self.positions_count_all = int(
            len([p for p in self.open_positions.values() if _as_int(p.get("total_count"), 0) > 0])
        )

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

        ops = st.get("open_positions")
        if isinstance(ops, dict):
            self.open_positions = {str(k): v for k, v in ops.items() if isinstance(k, str) and isinstance(v, dict)}

        self.market_cost = {str(k): float(v) for k, v in (st.get("market_cost") or {}).items() if isinstance(k, str)}
        self.event_cost = {str(k): float(v) for k, v in (st.get("event_cost") or {}).items() if isinstance(k, str)}
        self.event_positions = {str(k): int(v) for k, v in (st.get("event_positions") or {}).items() if isinstance(k, str)}
        self.total_cost_all = _as_float(st.get("total_cost_all"), 0.0)
        self.positions_count_all = _as_int(st.get("positions_count_all"), 0)

        oos = st.get("open_orders")
        if isinstance(oos, dict):
            self.open_orders = {str(k): v for k, v in oos.items() if isinstance(k, str) and isinstance(v, dict)}

        aobm = st.get("active_order_by_market")
        if isinstance(aobm, dict):
            self.active_order_by_market = {str(k): str(v) for k, v in aobm.items() if isinstance(k, str) and isinstance(v, str)}

        if not self.market_cost or not self.event_cost:
            self._recompute_aggregates_from_positions()

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
        if self.max_cost_per_market is not None:
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

    def _log_skip(self, cand: _ActionCandidate, *, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "event_ticker": cand.event_ticker,
            "market_ticker": cand.market_ticker,
            "side": cand.side,
            "source": cand.source,
            "price_cents": int(cand.price_cents),
            "fee_cents": int(cand.fee_cents),
            "p_yes": float(cand.p_yes),
            "edge_pp": float(cand.edge_pp),
            "ev": float(cand.ev),
            "skip_reason": str(reason),
            "dry_run": self.dry_run,
        }
        if extra:
            payload.update(extra)
        self.log.log("skip", payload)

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
        ev: float,
        source: str,
        order_id: str,
        strike: Optional[float],
        subtitle: Optional[str],
        implied_q_yes: Optional[float],
        ts_utc: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
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
            "ev": float(ev),
            "source": str(source),
            "order_id": str(order_id),
        }

        if pos is None:
            pos = {
                "market_ticker": str(market_ticker),
                "event_ticker": str(event_ticker),
                "side": str(side),
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
            return was_open, pos

        pos["event_ticker"] = str(event_ticker)
        pos["side"] = str(side)
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

        return was_open, pos

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
                    side=str(side),
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
                        side=str(side),
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
                str(existing.get("side"))
                if isinstance(existing, dict) and existing.get("side") in {"yes", "no"}
                else None
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
            if secs is not None and secs < float(self.order_refresh_seconds):
                continue
            try:
                tracked2, delta = self.om.refresh_tracked_order(tracked)
            except Exception as e:
                self.log.log("order_refresh_failed", {"order_id": oid, "error": str(e)})
                continue
            self.open_orders[oid] = tracked2
            if delta is not None and delta.delta_fill_count > 0:
                row = by_ticker.get(str(tracked2.get("market_ticker")))
                p_yes = float(row.p_model) if row is not None else float(tracked2.get("last_model_p") or 0.5)
                side = str(tracked2.get("side"))
                p_win = p_yes if side == "yes" else (1.0 - p_yes)
                price_cents = delta.avg_price_cents if delta.avg_price_cents is not None else _as_int(tracked2.get("price_cents"), 0)
                fee_cents = delta.avg_fee_cents if delta.avg_fee_cents is not None else (self.fee_cents_maker if str(tracked2.get("source")) == "maker" else self.fee_cents_taker)
                edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))
                was_open, pos_after = self._apply_fill(
                    market_ticker=str(tracked2.get("market_ticker")),
                    event_ticker=str(tracked2.get("event_ticker")),
                    side=side,
                    fill_count=int(delta.delta_fill_count),
                    price_cents=int(price_cents),
                    fee_cents=int(fee_cents),
                    p_yes=float(p_yes),
                    edge_pp=float(edge_pp),
                    ev=float(edge_pp),
                    source=str(tracked2.get("source")),
                    order_id=str(tracked2.get("order_id")),
                    strike=float(row.strike) if row is not None else None,
                    subtitle=str(row.subtitle) if row is not None else None,
                    implied_q_yes=(float(row.ob.ybuy) / 100.0) if (row is not None and row.ob.ybuy is not None) else None,
                    ts_utc=delta.ts_utc,
                )
                self.log.log("fill_detected", {"order_id": oid, "delta_fill_count": int(delta.delta_fill_count), "was_open": bool(was_open), "position_total_count": int(_as_int(pos_after.get("total_count"), 0))})
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
        self.log.log("order_cancel_submit", {"order_id": oid, "reason": str(reason)})
        try:
            resp = self.om.submit_cancel(tracked)
            self.log.log("order_canceled", {"order_id": oid, "resp": resp})
        except Exception as e:
            self.log.log("order_cancel_failed", {"order_id": oid, "error": str(e)})
            return False
        self._cleanup_order_refs(oid)
        return True

    def _amend_order(self, tracked: Dict[str, Any], *, new_price_cents: int, new_count: int) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log("order_amend_submit", {"order_id": oid, "new_price_cents": int(new_price_cents), "new_count": int(new_count)})
        try:
            resp = self.om.submit_amend(tracked, new_price_cents=int(new_price_cents), new_count=int(new_count))
            self.open_orders[oid] = tracked
            self.log.log("order_amended", {"order_id": oid, "resp": resp})
            return True
        except Exception as e:
            self.log.log("order_amend_failed", {"order_id": oid, "error": str(e)})
            return False

    def _create_order_for_candidate(self, cand: _ActionCandidate, *, remaining_to_target: int, current_contracts: int, target_contracts: int) -> bool:
        tif = self.taker_time_in_force if cand.source == "taker" else self.maker_time_in_force
        key = market_side_key(cand.market_ticker, cand.side)
        self.log.log("order_submit", {"mode": cand.source, "ticker": cand.market_ticker, "side": cand.side, "count": int(remaining_to_target), "price_cents": int(cand.price_cents), "time_in_force": str(tif)})
        tracked, _ = self.om.submit_new_order(
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
        )
        oid = str(tracked.get("order_id"))
        self.open_orders[oid] = tracked
        self.active_order_by_market[key] = oid

        # Apply immediate fills immediately.
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
            was_open, _ = self._apply_fill(
                market_ticker=cand.market_ticker,
                event_ticker=cand.event_ticker,
                side=cand.side,
                fill_count=int(delta.delta_fill_count),
                price_cents=int(price_cents),
                fee_cents=int(fee_cents),
                p_yes=float(p_yes),
                edge_pp=float(edge_pp),
                ev=float(edge_pp),
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

    def on_tick(self, result: EvaluationResult) -> None:
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
            row = by_ticker.get(str(tracked.get("market_ticker")))
            reason = self._should_cancel_resting(tracked, row)
            if reason is not None and self._cancel_order(tracked, reason=reason):
                changed = True

        cands = self._build_candidates(result)
        submitted = 0

        for cand in cands:
            if submitted >= int(self.max_entries_per_tick):
                break

            pos = self.open_positions.get(cand.market_ticker)
            if pos is not None and str(pos.get("side")) and str(pos.get("side")) != str(cand.side):
                self._log_skip(cand, reason="position_side_conflict")
                continue

            current, target, size_reason = self._target_contracts_for_candidate(pos, cand)
            remaining = int(target - current)
            if remaining <= 0:
                self._log_skip(cand, reason="target_reached")
                continue
            if size_reason is not None:
                self._log_skip(cand, reason=size_reason)
                continue

            key = market_side_key(cand.market_ticker, cand.side)
            existing_oid = self.active_order_by_market.get(key)
            if existing_oid and existing_oid in self.open_orders:
                tracked = self.open_orders[existing_oid]
                if str(tracked.get("source")) == "maker" and str(tracked.get("status", "")).lower() == "resting":
                    cur_price = _as_int(tracked.get("price_cents"), 0)
                    cur_rem = _as_int(tracked.get("remaining_count"), _as_int(tracked.get("count"), 0))
                    if cur_price == int(cand.price_cents) and cur_rem == int(remaining):
                        self._log_skip(cand, reason="already_has_matching_resting_order", extra={"order_id": existing_oid})
                        continue
                    tracked["last_model_p"] = float(cand.p_yes)
                    tracked["last_edge_pp"] = float(cand.edge_pp)
                    if self._amend_order(tracked, new_price_cents=int(cand.price_cents), new_count=int(remaining)):
                        changed = True
                        submitted += 1
                        continue
                    if self._cancel_order(tracked, reason="amend_failed_replace"):
                        changed = True
                        existing_oid = None

                self._log_skip(cand, reason="already_has_active_order", extra={"order_id": existing_oid})
                continue

            add_cost = float(remaining) * ((float(cand.price_cents) + float(cand.fee_cents)) / 100.0)
            evt = cand.event_ticker
            is_new_market_in_event = int(_as_int(pos.get("total_count"), 0) if pos else 0) <= 0
            cap_reason = self._cap_check(event_ticker=evt, market_ticker=cand.market_ticker, add_cost_dollars=add_cost, is_new_market_in_event=is_new_market_in_event)
            if cap_reason is not None:
                self._log_skip(cand, reason=cap_reason)
                continue

            if self._create_order_for_candidate(cand, remaining_to_target=remaining, current_contracts=current, target_contracts=target):
                changed = True
                submitted += 1

        if changed:
            self._persist_state_file()

        print(
            f"[TRADE] tick event={result.event_ticker} candidates={len(cands)} entries={submitted} "
            f"total_cost_all=${self.total_cost_all:.4f} positions_all={self.positions_count_all} open_orders={len(self.open_orders)}"
        )

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "positions_count_all": int(self.positions_count_all),
            "total_cost_all": float(self.total_cost_all),
            "event_cost": self.event_cost,
            "event_positions": self.event_positions,
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "active_order_by_market": self.active_order_by_market,
        }

    def on_shutdown(self, last_result: Optional[EvaluationResult] = None) -> None:
        snap = self.snapshot_state()
        if last_result is not None:
            snap.update({"spot": float(last_result.market_state.spot), "minutes_left": float(last_result.minutes_left)})
        self.log.log("bot_shutdown", snap)


# Final canonical re-export (wins over any legacy code above).
from kalshi_edge.trader_v2_engine import V2Trader as V2Trader  # noqa: E402,F401
from kalshi_edge.trader_v2_engine import debug_order_manager as debug_order_manager  # noqa: E402,F401



# -------------------------------------------------------------------
# IMPORTANT: canonical v2.2 engine re-export
#
# `trader_v2.py` has historically been edited a lot and can contain legacy
# definitions; ensure the *final* exported symbols point to the clean v2.2
# engine implementation.
# -------------------------------------------------------------------

from kalshi_edge.trader_v2_engine import V2Trader as V2Trader  # noqa: E402,F401
from kalshi_edge.trader_v2_engine import debug_order_manager as debug_order_manager  # noqa: E402,F401

class _LegacyGarbage:
    def reconcile_state(self, event_ticker: str) -> None:
        event_ticker = str(event_ticker).upper()
        if self.dry_run:
            self.log.log("reconcile_skipped", {"event_ticker": event_ticker, "reason": "dry_run"})
            return
        try:
            from kalshi_edge.kalshi_api import get_positions
        except Exception as e:
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return
        cursor = None
        market_positions: List[dict] = []
        try:
            while True:
                resp = get_positions(self.http, self.auth, base_url=self.kalshi_base_url, event_ticker=event_ticker, limit=1000, cursor=cursor, count_filter="position")
                market_positions.extend(resp.get("market_positions") or [])
                cursor = resp.get("cursor")
                if not cursor:
                    break
        except Exception as e:
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return
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
                "strike": None,
                "subtitle": None,
                "implied_q_yes": None,
                "reconciled": True,
            }
            added += 1
        if added > 0:
            self._recompute_aggregates_from_positions()
            self._persist_state_file()
        self.log.log("reconcile_done", {"event_ticker": event_ticker, "positions_added": int(added), "positions_count_all": int(self.positions_count_all)})


def debug_order_manager() -> None:
    """
    Tiny debug routine (no network) that simulates:
    - two ticks with the same market -> only one active order exists
    - a fill -> position count increases
    - a model p move beyond p_requote_pp -> resting order cancels
    """
    from dataclasses import dataclass

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
        sigma_blend: float = 0.0

    t = V2Trader(
        http=HttpClient(debug=False),
        auth=KalshiAuth(api_key_id="DUMMY", private_key_path="/dev/null"),
        kalshi_base_url="https://example.invalid",
        state_file=".debug_state_v22.json",
        trade_log_file="logs/debug_order_manager.jsonl",
        dry_run=True,
        order_mode="maker_only",
        post_only=True,
        max_contracts_per_market=2,
        allow_scale_in=True,
        p_requote_pp=0.02,
        cancel_stale_seconds=99999,
        max_entries_per_tick=1,
        fee_cents_maker=1,
        fee_cents_taker=1,
        ev_min=0.05,
    )

    row1 = _Row(
        ticker="TEST-MKT",
        p_model=0.70,
        strike=123.0,
        subtitle="debug",
        ob=_OB(ybid=50, nbid=50, ybuy=52, nbuy=52, spread_y=2, spread_n=2, yqty=10.0, nqty=10.0),
    )
    res1 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row1])  # type: ignore[arg-type]
    t.on_tick(res1)
    assert len(t.active_order_by_market) <= 1, "should have <=1 active order after tick 1"

    before = dict(t.active_order_by_market)
    t.on_tick(res1)
    assert t.active_order_by_market == before, "should not spam duplicate orders in watch mode"

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
        ev=0.70 - (float(int(cand_price) + 1) / 100.0),
        source="maker",
        order_id="SIMULATED",
        strike=123.0,
        subtitle="debug",
        implied_q_yes=0.52,
    )
    assert t.open_positions["TEST-MKT"]["total_count"] == 1, "position should increase after fill"

    row2 = _Row(
        ticker="TEST-MKT",
        p_model=0.67,
        strike=123.0,
        subtitle="debug",
        ob=row1.ob,
    )
    res2 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row2])  # type: ignore[arg-type]
    t.on_tick(res2)
    assert len(t.active_order_by_market) <= 1, "should still have <=1 active order after requote"

    print("[DEBUG] order manager simulation OK")

"""
trader_v2.py

V2.2 trader: hold-to-expiration entry engine with resting-order support.

Key properties:
- Entry only (BUY): never submits exits.
- Multi-event state: can trade across discovered events (optional lock in run.py).
- Strategy iteration engine: maintains at most ONE active order per (market_ticker, side).
- Hybrid maker/taker: can place post-only resting orders when taker price isn't good enough.
- Scale-in is explicit via target sizing + caps; no duplicate order spam in watch mode.
"""

# from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.strategy_config import DEFAULT_CONFIG as _DEFAULT_CFG

EV_MIN = _DEFAULT_CFG.MIN_EV
ORDER_SIZE = _DEFAULT_CFG.ORDER_SIZE
MIN_TOP_SIZE = _DEFAULT_CFG.MIN_TOP_SIZE
SPREAD_MAX_CENTS = _DEFAULT_CFG.SPREAD_MAX_CENTS
MAX_POSITIONS_PER_EVENT = _DEFAULT_CFG.MAX_POSITIONS_PER_EVENT
MAX_COST_PER_EVENT = _DEFAULT_CFG.MAX_COST_PER_EVENT
MAX_COST_PER_STRIKE = _DEFAULT_CFG.MAX_COST_PER_MARKET
from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.order_manager import FillDelta, OrderManager, market_side_key, utc_ts
from kalshi_edge.pipeline import EvaluationResult
from kalshi_edge.trade_log import TradeLogger


SCHEMA = "v2.2"


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


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _secs_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


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
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        # base knobs
        count: int = ORDER_SIZE,
        ev_min: float = EV_MIN,
        min_top_size: float = MIN_TOP_SIZE,
        spread_max_cents: Optional[int] = SPREAD_MAX_CENTS,
        max_positions_per_event: int = MAX_POSITIONS_PER_EVENT,
        max_cost_per_event: float = MAX_COST_PER_EVENT,
        dry_run: bool = False,
        # caps
        max_cost_per_market: Optional[float] = None,
        max_total_cost: Optional[float] = None,
        max_total_positions: Optional[int] = None,
        # order behavior
        order_mode: str = "hybrid",  # taker_only | maker_only | hybrid
        post_only: bool = True,
        maker_time_in_force: str = "good_till_canceled",
        taker_time_in_force: str = "fill_or_kill",
        order_refresh_seconds: int = 10,
        cancel_stale_seconds: int = 60,
        p_requote_pp: float = 0.02,
        max_entries_per_tick: int = 1,
        max_contracts_per_market: int = 1,
        allow_scale_in: Optional[bool] = None,
        scale_in_cooldown_seconds: int = 60,
        min_edge_pp_entry: Optional[float] = None,
        min_edge_pp_scale_in: Optional[float] = None,
        maker_extra_buffer: float = 0.01,
        fee_cents_taker: int = 1,
        fee_cents_maker: int = 1,
        subaccount: Optional[int] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(trade_log_file)

        self.count = int(count)
        self.ev_min = float(ev_min)
        self.min_top_size = float(min_top_size)
        self.spread_max_cents = int(spread_max_cents) if spread_max_cents is not None else None

        self.max_positions_per_event = int(max_positions_per_event)
        self.max_cost_per_event = float(max_cost_per_event)
        self.max_cost_per_market = float(max_cost_per_market) if max_cost_per_market is not None else None
        self.max_total_cost = float(max_total_cost) if max_total_cost is not None else None
        self.max_total_positions = int(max_total_positions) if max_total_positions is not None else None

        self.order_mode = str(order_mode)
        self.post_only = bool(post_only)
        self.maker_time_in_force = str(maker_time_in_force)
        self.taker_time_in_force = str(taker_time_in_force)
        self.order_refresh_seconds = int(order_refresh_seconds)
        self.cancel_stale_seconds = int(cancel_stale_seconds)
        self.p_requote_pp = float(p_requote_pp)
        self.max_entries_per_tick = int(max_entries_per_tick)

        self.max_contracts_per_market = int(max_contracts_per_market)
        if allow_scale_in is None:
            allow_scale_in = self.max_contracts_per_market > 1
        self.allow_scale_in = bool(allow_scale_in)
        self.scale_in_cooldown_seconds = int(scale_in_cooldown_seconds)

        self.min_edge_pp_entry = float(min_edge_pp_entry) if min_edge_pp_entry is not None else float(self.ev_min)
        self.min_edge_pp_scale_in = (
            float(min_edge_pp_scale_in) if min_edge_pp_scale_in is not None else float(self.ev_min + 0.01)
        )
        self.maker_extra_buffer = float(maker_extra_buffer)

        self.fee_cents_taker = int(fee_cents_taker)
        self.fee_cents_maker = int(fee_cents_maker)
        self.dry_run = bool(dry_run)

        self.om = OrderManager(
            http=self.http,
            auth=self.auth,
            kalshi_base_url=self.kalshi_base_url,
            log=self.log,
            dry_run=self.dry_run,
            subaccount=subaccount,
        )

        # ---- state (v2.2) ----
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.market_cost: Dict[str, float] = {}
        self.event_cost: Dict[str, float] = {}
        self.event_positions: Dict[str, int] = {}
        self.total_cost_all: float = 0.0
        self.positions_count_all: int = 0

        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.active_order_by_market: Dict[str, str] = {}  # "TICKER|side" -> order_id

        self._load_from_state_file()

        self.log.log(
            "bot_start",
            {
                "schema": SCHEMA,
                "state_file": self.state_file,
                "trade_log_file": trade_log_file,
                "count": self.count,
                "ev_min": self.ev_min,
                "min_top_size": self.min_top_size,
                "spread_max_cents": self.spread_max_cents,
                "max_positions_per_event": self.max_positions_per_event,
                "max_cost_per_event": self.max_cost_per_event,
                "max_cost_per_market": self.max_cost_per_market,
                "max_total_cost": self.max_total_cost,
                "max_total_positions": self.max_total_positions,
                "order_mode": self.order_mode,
                "post_only": self.post_only,
                "maker_time_in_force": self.maker_time_in_force,
                "taker_time_in_force": self.taker_time_in_force,
                "order_refresh_seconds": self.order_refresh_seconds,
                "cancel_stale_seconds": self.cancel_stale_seconds,
                "p_requote_pp": self.p_requote_pp,
                "max_entries_per_tick": self.max_entries_per_tick,
                "max_contracts_per_market": self.max_contracts_per_market,
                "allow_scale_in": self.allow_scale_in,
                "scale_in_cooldown_seconds": self.scale_in_cooldown_seconds,
                "min_edge_pp_entry": self.min_edge_pp_entry,
                "min_edge_pp_scale_in": self.min_edge_pp_scale_in,
                "maker_extra_buffer": self.maker_extra_buffer,
                "fee_cents_taker": self.fee_cents_taker,
                "fee_cents_maker": self.fee_cents_maker,
                "dry_run": self.dry_run,
            },
        )

    # --------------------
    # state
    # --------------------

    def _persist_state_file(self) -> None:
        out = {
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
        }
        _write_state(self.state_file, out)

    def _recompute_aggregates_from_positions(self) -> None:
        self.market_cost = {}
        self.event_cost = {}
        self.event_positions = {}
        total_cost = 0.0

        for mkt, pos in self.open_positions.items():
            tc = _as_int(pos.get("total_count"), 0)
            if tc <= 0:
                continue
            evt = str(pos.get("event_ticker") or "")
            cost = _as_float(pos.get("total_cost_dollars"), 0.0)
            self.market_cost[str(mkt)] = float(cost)
            if evt:
                self.event_cost[evt] = float(self.event_cost.get(evt, 0.0) + cost)
                self.event_positions[evt] = int(self.event_positions.get(evt, 0) + 1)
            total_cost += cost

        self.total_cost_all = float(total_cost)
        self.positions_count_all = int(
            len([p for p in self.open_positions.values() if _as_int(p.get("total_count"), 0) > 0])
        )

    def _migrate_v2_to_v22(self, st: Dict[str, Any]) -> None:
        ops = st.get("open_positions")
        if not isinstance(ops, dict):
            return

        migrated_positions: Dict[str, Dict[str, Any]] = {}
        for market_ticker, old in ops.items():
            if not isinstance(market_ticker, str) or not isinstance(old, dict):
                continue
            event_ticker = str(old.get("event_ticker") or st.get("event_ticker") or "")
            side = str(old.get("side") or "")
            count = _as_int(old.get("count"), 0)
            entry_price_cents = old.get("entry_price_cents")
            price_cents = _as_int(entry_price_cents, 0) if isinstance(entry_price_cents, int) else 0
            entry_cost = _as_float(old.get("entry_cost_dollars"), 0.0)
            entry_fee = _as_float(old.get("entry_fee_dollars"), 0.0)
            p_yes = old.get("p_at_entry")
            edge_pp = old.get("edge_pp_at_entry")
            ev = old.get("ev_at_entry")

            fill_id = "migrate-" + str(uuid.uuid4())
            ts = str(old.get("entry_ts_utc") or utc_ts())

            fills = [
                {
                    "fill_id": fill_id,
                    "ts_utc": ts,
                    "count": int(count),
                    "price_cents": int(price_cents),
                    "fee_cents": int(round((entry_fee * 100.0) / float(count))) if count > 0 else 0,
                    "cost_dollars": float(entry_cost),
                    "p_yes": float(p_yes) if isinstance(p_yes, (int, float)) else None,
                    "edge_pp": float(edge_pp) if isinstance(edge_pp, (int, float)) else None,
                    "ev": float(ev) if isinstance(ev, (int, float)) else None,
                    "source": "migrated",
                    "order_id": None,
                }
            ]

            migrated_positions[market_ticker] = {
                "market_ticker": market_ticker,
                "event_ticker": event_ticker,
                "side": side,
                "total_count": int(count),
                "total_cost_dollars": float(entry_cost),
                "total_fee_dollars": float(entry_fee),
                "fills": fills,
                "last_fill_ts_utc": ts,
                "last_fill_price_cents": int(price_cents) if price_cents else None,
                "last_fill_edge_pp": float(edge_pp) if isinstance(edge_pp, (int, float)) else None,
                "strike": old.get("strike"),
                "subtitle": old.get("subtitle"),
                "implied_q_yes": old.get("implied_q_yes"),
                "migrated_from": "v2",
            }

        self.open_positions = migrated_positions
        self.open_orders = {}
        self.active_order_by_market = {}
        self._recompute_aggregates_from_positions()
        self._persist_state_file()

    def _load_from_state_file(self) -> None:
        st = _read_state(self.state_file)
        if not isinstance(st, dict):
            return

        schema = st.get("schema")
        if schema == "v2":
            self._migrate_v2_to_v22(st)
            return
        if schema != SCHEMA:
            return

        ops = st.get("open_positions")
        if isinstance(ops, dict):
            self.open_positions = {str(k): v for k, v in ops.items() if isinstance(k, str) and isinstance(v, dict)}

        self.market_cost = {
            str(k): float(v)
            for k, v in (st.get("market_cost") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
        self.event_cost = {
            str(k): float(v)
            for k, v in (st.get("event_cost") or {}).items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
        self.event_positions = {
            str(k): int(v) for k, v in (st.get("event_positions") or {}).items() if isinstance(k, str)
        }
        self.total_cost_all = _as_float(st.get("total_cost_all"), 0.0)
        self.positions_count_all = _as_int(st.get("positions_count_all"), 0)

        oos = st.get("open_orders")
        if isinstance(oos, dict):
            self.open_orders = {str(k): v for k, v in oos.items() if isinstance(k, str) and isinstance(v, dict)}

        aobm = st.get("active_order_by_market")
        if isinstance(aobm, dict):
            self.active_order_by_market = {
                str(k): str(v) for k, v in aobm.items() if isinstance(k, str) and isinstance(v, str)
            }

        if not self.market_cost or not self.event_cost:
            self._recompute_aggregates_from_positions()

    # --------------------
    # caps + sizing
    # --------------------

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

        if self.max_cost_per_market is not None:
            if (float(self.market_cost.get(market_ticker, 0.0)) + float(add_cost_dollars)) > float(
                self.max_cost_per_market
            ):
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
        last_ts = pos.get("last_fill_ts_utc")
        if not isinstance(last_ts, str) or not last_ts:
            return True
        secs = _secs_since(last_ts)
        if secs is None:
            return True
        return secs >= float(self.scale_in_cooldown_seconds)

    def _target_contracts_for_candidate(self, pos: Optional[Dict[str, Any]], cand: _ActionCandidate) -> Tuple[int, int, Optional[str]]:
        current = _as_int(pos.get("total_count"), 0) if pos else 0
        if current <= 0:
            if cand.edge_pp < float(self.min_edge_pp_entry):
                return current, current, "edge_below_min_entry"
            target = min(int(self.max_contracts_per_market), int(current + self.count))
            return current, int(target), None

        if not self.allow_scale_in:
            return current, current, "scale_in_disabled"
        if cand.edge_pp < float(self.min_edge_pp_scale_in):
            return current, current, "edge_below_min_scale_in"
        if not self._cooldown_ok(pos):
            return current, current, "scale_in_cooldown"

        target = min(int(self.max_contracts_per_market), int(current + self.count))
        return current, int(target), None

    # --------------------
    # logging
    # --------------------

    def _log_skip(self, cand: _ActionCandidate, *, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "event_ticker": cand.event_ticker,
            "market_ticker": cand.market_ticker,
            "side": cand.side,
            "source": cand.source,
            "price_cents": int(cand.price_cents),
            "fee_cents": int(cand.fee_cents),
            "p_yes": float(cand.p_yes),
            "implied_q_yes": float(cand.implied_q_yes) if cand.implied_q_yes is not None else None,
            "edge_pp": float(cand.edge_pp),
            "ev": float(cand.ev),
            "max_price_cents": int(cand.max_price_cents),
            "bid": int(cand.bid_cents) if cand.bid_cents is not None else None,
            "ask_proxy": int(cand.ask_proxy_cents) if cand.ask_proxy_cents is not None else None,
            "spread_cents": int(cand.spread_cents) if cand.spread_cents is not None else None,
            "top_size": float(cand.top_size) if cand.top_size is not None else None,
            "skip_reason": str(reason),
            "dry_run": self.dry_run,
        }
        if extra:
            payload.update(extra)
        self.log.log("skip", payload)

    # --------------------
    # fills -> positions
    # --------------------

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
        ev: float,
        source: str,
        order_id: str,
        strike: Optional[float],
        subtitle: Optional[str],
        implied_q_yes: Optional[float],
        ts_utc: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
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
            "ev": float(ev),
            "source": str(source),
            "order_id": str(order_id),
        }

        if pos is None:
            pos = {
                "market_ticker": str(market_ticker),
                "event_ticker": str(event_ticker),
                "side": str(side),
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
            self.log.log(
                "fill_side_conflict",
                {
                    "market_ticker": market_ticker,
                    "existing_side": pos.get("side"),
                    "fill_side": side,
                    "order_id": order_id,
                },
            )
            return was_open, pos

        pos["event_ticker"] = str(event_ticker)
        pos["side"] = str(side)
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

        return was_open, pos

    # --------------------
    # candidate building
    # --------------------

    def _liquidity_check(self, spread_cents: Optional[int], top_size: Optional[float]) -> Optional[str]:
        if self.spread_max_cents is not None and spread_cents is not None and int(spread_cents) > int(self.spread_max_cents):
            return "spread_too_wide"
        if top_size is not None and float(top_size) < float(self.min_top_size):
            return "top_size_too_small"
        return None

    def _max_acceptable_price_cents(self, *, p_win: float, fee_buffer_cents: int) -> int:
        x = int(math.floor(100.0 * (float(p_win) - float(self.ev_min)))) - int(fee_buffer_cents)
        return _clamp_int(x, 0, 99)

    def _edge_at_price(self, *, p_win: float, price_cents: int, fee_cents: int) -> float:
        return float(p_win) - (float(price_cents + fee_cents) / 100.0)

    def _best_action_for_side(
        self,
        row,
        *,
        event_ticker: str,
        side: str,
        existing_side: Optional[str],
    ) -> Optional[_ActionCandidate]:
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

        # TAKE NOW (taker)
        if self.order_mode in {"taker_only", "hybrid"} and ask_proxy is not None:
            max_price = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_taker)
            liq_reason = self._liquidity_check(spread, top_size)
            if liq_reason is None and int(ask_proxy) <= int(max_price):
                ev_take = self._edge_at_price(p_win=p_win, price_cents=int(ask_proxy), fee_cents=self.fee_cents_taker)
                best = _ActionCandidate(
                    market_ticker=str(row.ticker),
                    event_ticker=str(event_ticker),
                    side=str(side),
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

        # MAKE (resting maker)
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
                        side=str(side),
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
                str(existing.get("side"))
                if isinstance(existing, dict) and existing.get("side") in {"yes", "no"}
                else None
            )

            c_yes = self._best_action_for_side(
                row, event_ticker=str(result.event_ticker), side="yes", existing_side=existing_side
            )
            c_no = self._best_action_for_side(
                row, event_ticker=str(result.event_ticker), side="no", existing_side=existing_side
            )

            best = None
            if c_yes is not None:
                best = c_yes
            if c_no is not None and (best is None or c_no.ev > best.ev):
                best = c_no

            if best is not None:
                out.append(best)

        out.sort(key=lambda c: c.ev, reverse=True)
        return out

    # --------------------
    # order management
    # --------------------

    def _cleanup_order_refs(self, order_id: str) -> None:
        to_del = [k for k, v in self.active_order_by_market.items() if v == order_id]
        for k in to_del:
            self.active_order_by_market.pop(k, None)
        self.open_orders.pop(order_id, None)

    def refresh_orders_and_apply_fills(self, result: EvaluationResult) -> bool:
        changed = False
        by_ticker = {str(r.ticker): r for r in result.rows}

        for order_id in list(self.open_orders.keys()):
            tracked = self.open_orders.get(order_id)
            if not isinstance(tracked, dict):
                self.open_orders.pop(order_id, None)
                changed = True
                continue

            status = str(tracked.get("status", "")).lower()
            if _is_terminal(status):
                self._cleanup_order_refs(order_id)
                changed = True
                continue

            secs = _secs_since(tracked.get("last_checked_ts_utc"))
            if secs is not None and secs < float(self.order_refresh_seconds):
                continue

            try:
                tracked2, delta = self.om.refresh_tracked_order(tracked)
            except Exception as e:
                self.log.log("order_refresh_failed", {"order_id": order_id, "error": str(e)})
                continue

            self.open_orders[order_id] = tracked2

            if delta is not None and delta.delta_fill_count > 0:
                row = by_ticker.get(str(tracked2.get("market_ticker")))
                p_yes = float(row.p_model) if row is not None else float(tracked2.get("last_model_p") or 0.5)
                side = str(tracked2.get("side"))
                p_win = p_yes if side == "yes" else (1.0 - p_yes)
                price_cents = delta.avg_price_cents if delta.avg_price_cents is not None else _as_int(tracked2.get("price_cents"), 0)
                fee_cents = delta.avg_fee_cents if delta.avg_fee_cents is not None else (
                    int(self.fee_cents_maker) if str(tracked2.get("source")) == "maker" else int(self.fee_cents_taker)
                )
                edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))

                pos_before = self.open_positions.get(str(tracked2.get("market_ticker")))
                was_open = pos_before is not None and _as_int(pos_before.get("total_count"), 0) > 0

                is_scale_in, pos_after = self._apply_fill(
                    market_ticker=str(tracked2.get("market_ticker")),
                    event_ticker=str(tracked2.get("event_ticker")),
                    side=side,
                    fill_count=int(delta.delta_fill_count),
                    price_cents=int(price_cents),
                    fee_cents=int(fee_cents),
                    p_yes=float(p_yes),
                    edge_pp=float(edge_pp),
                    ev=float(edge_pp),
                    source=str(tracked2.get("source")),
                    order_id=str(tracked2.get("order_id")),
                    strike=float(row.strike) if row is not None else None,
                    subtitle=str(row.subtitle) if row is not None else None,
                    implied_q_yes=(float(row.ob.ybuy) / 100.0) if (row is not None and row.ob.ybuy is not None) else None,
                    ts_utc=delta.ts_utc,
                )

                self.log.log(
                    "fill_detected",
                    {
                        "order_id": str(tracked2.get("order_id")),
                        "market_ticker": str(tracked2.get("market_ticker")),
                        "event_ticker": str(tracked2.get("event_ticker")),
                        "side": side,
                        "source": str(tracked2.get("source")),
                        "delta_fill_count": int(delta.delta_fill_count),
                        "delta_cost_cents": int(delta.delta_cost_cents),
                        "delta_fee_cents": int(delta.delta_fee_cents),
                        "avg_price_cents": int(price_cents),
                        "avg_fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "was_open": bool(was_open),
                        "is_scale_in": bool(is_scale_in),
                        "position_total_count": int(_as_int(pos_after.get("total_count"), 0)),
                    },
                )

                self.log.log(
                    "scale_in_filled" if is_scale_in else "entry_filled",
                    {
                        "order_id": str(tracked2.get("order_id")),
                        "market_ticker": str(tracked2.get("market_ticker")),
                        "event_ticker": str(tracked2.get("event_ticker")),
                        "side": side,
                        "count": int(delta.delta_fill_count),
                        "price_cents": int(price_cents),
                        "fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "ev": float(edge_pp),
                        "dry_run": self.dry_run,
                    },
                )

                changed = True

            status2 = str(tracked2.get("status", "")).lower()
            if _is_terminal(status2) or _as_int(tracked2.get("remaining_count"), 0) <= 0:
                self._cleanup_order_refs(order_id)
                changed = True

        return changed

    def _should_cancel_resting(self, tracked: Dict[str, Any], row) -> Optional[str]:
        status = str(tracked.get("status", "")).lower()
        if status != "resting":
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
        fee_cents = int(self.fee_cents_maker) if str(tracked.get("source")) == "maker" else int(self.fee_cents_taker)
        edge_now = self._edge_at_price(p_win=p_win, price_cents=price_cents, fee_cents=fee_cents)
        if edge_now < float(self.ev_min + self.maker_extra_buffer):
            return "maker_edge_too_low_now"

        return None

    def _cancel_order(self, tracked: Dict[str, Any], *, reason: str) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log(
            "order_cancel_submit",
            {
                "order_id": oid,
                "market_ticker": tracked.get("market_ticker"),
                "event_ticker": tracked.get("event_ticker"),
                "side": tracked.get("side"),
                "reason": str(reason),
                "dry_run": self.dry_run,
            },
        )
        try:
            resp = self.om.submit_cancel(tracked)
            self.log.log("order_canceled", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
        except Exception as e:
            self.log.log("order_cancel_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False

        self._cleanup_order_refs(oid)
        return True

    def _amend_order(self, tracked: Dict[str, Any], *, new_price_cents: int, new_count: int) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log(
            "order_amend_submit",
            {
                "order_id": oid,
                "market_ticker": tracked.get("market_ticker"),
                "event_ticker": tracked.get("event_ticker"),
                "side": tracked.get("side"),
                "new_price_cents": int(new_price_cents),
                "new_count": int(new_count),
                "dry_run": self.dry_run,
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
        current_contracts: int,
        target_contracts: int,
        is_scale_in_attempt: bool,
    ) -> bool:
        tif = self.taker_time_in_force if cand.source == "taker" else self.maker_time_in_force
        key = market_side_key(cand.market_ticker, cand.side)

        self.log.log(
            "order_submit",
            {
                "mode": cand.source,
                "post_only": bool(self.post_only if cand.source == "maker" else False),
                "time_in_force": str(tif),
                "event_ticker": cand.event_ticker,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(remaining_to_target),
                "price_cents": int(cand.price_cents),
                "fee_cents": int(cand.fee_cents),
                "max_price_cents": int(cand.max_price_cents),
                "bid": int(cand.bid_cents) if cand.bid_cents is not None else None,
                "ask_proxy": int(cand.ask_proxy_cents) if cand.ask_proxy_cents is not None else None,
                "p_yes": float(cand.p_yes),
                "edge_pp": float(cand.edge_pp),
                "ev": float(cand.ev),
                "is_scale_in_attempt": bool(is_scale_in_attempt),
                "current_contracts": int(current_contracts),
                "target_contracts": int(target_contracts),
                "remaining_to_target": int(remaining_to_target),
                "dry_run": self.dry_run,
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
        )

        oid = str(tracked.get("order_id"))
        self.open_orders[oid] = tracked
        self.active_order_by_market[key] = oid

        status = str(tracked.get("status", "")).lower()
        if status == "resting":
            self.log.log(
                "order_resting",
                {"order_id": oid, "market_ticker": cand.market_ticker, "side": cand.side, "dry_run": self.dry_run},
            )

        # Apply immediate fills right away (don’t wait for refresh cadence).
        fc = _as_int(tracked.get("fill_count"), 0)
        if fc > 0:
            total_cost = _as_int(tracked.get("last_fill_cost_cents"), 0)
            total_fee = _as_int(tracked.get("last_fee_paid_cents"), 0)
            delta = FillDelta(
                delta_fill_count=int(fc),
                delta_cost_cents=int(total_cost),
                delta_fee_cents=int(total_fee),
                ts_utc=utc_ts(),
            )
            price_cents = delta.avg_price_cents if delta.avg_price_cents is not None else int(cand.price_cents)
            fee_cents = delta.avg_fee_cents if delta.avg_fee_cents is not None else int(cand.fee_cents)
            p_yes = float(cand.p_yes)
            p_win = p_yes if cand.side == "yes" else (1.0 - p_yes)
            edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))
            is_scale_in, pos_after = self._apply_fill(
                market_ticker=cand.market_ticker,
                event_ticker=cand.event_ticker,
                side=cand.side,
                fill_count=int(delta.delta_fill_count),
                price_cents=int(price_cents),
                fee_cents=int(fee_cents),
                p_yes=float(p_yes),
                edge_pp=float(edge_pp),
                ev=float(edge_pp),
                source=str(cand.source),
                order_id=str(oid),
                strike=float(cand.strike),
                subtitle=str(cand.subtitle),
                implied_q_yes=cand.implied_q_yes,
                ts_utc=delta.ts_utc,
            )
            self.log.log(
                "fill_detected",
                {
                    "order_id": oid,
                    "market_ticker": cand.market_ticker,
                    "event_ticker": cand.event_ticker,
                    "side": cand.side,
                    "source": cand.source,
                    "delta_fill_count": int(delta.delta_fill_count),
                    "delta_cost_cents": int(delta.delta_cost_cents),
                    "delta_fee_cents": int(delta.delta_fee_cents),
                    "avg_price_cents": int(price_cents),
                    "avg_fee_cents": int(fee_cents),
                    "p_yes": float(p_yes),
                    "edge_pp": float(edge_pp),
                    "is_scale_in": bool(is_scale_in),
                    "position_total_count": int(_as_int(pos_after.get("total_count"), 0)),
                },
            )
            self.log.log(
                "scale_in_filled" if is_scale_in else "entry_filled",
                {
                    "order_id": oid,
                    "market_ticker": cand.market_ticker,
                    "event_ticker": cand.event_ticker,
                    "side": cand.side,
                    "count": int(delta.delta_fill_count),
                    "price_cents": int(price_cents),
                    "fee_cents": int(fee_cents),
                    "p_yes": float(p_yes),
                    "edge_pp": float(edge_pp),
                    "ev": float(edge_pp),
                    "dry_run": self.dry_run,
                },
            )
            if _as_int(tracked.get("remaining_count"), 0) <= 0 or _is_terminal(status):
                self._cleanup_order_refs(oid)

        return True

    # --------------------
    # main tick
    # --------------------

    def on_tick(self, result: EvaluationResult) -> None:
        changed = False

        # (1) refresh and apply fills
        if self.refresh_orders_and_apply_fills(result):
            changed = True

        # adverse selection guard for active resting maker orders
        by_ticker = {str(r.ticker): r for r in result.rows}
        for key, oid in list(self.active_order_by_market.items()):
            tracked = self.open_orders.get(oid)
            if not isinstance(tracked, dict):
                self.active_order_by_market.pop(key, None)
                changed = True
                continue
            if str(tracked.get("source")) != "maker":
                continue
            row = by_ticker.get(str(tracked.get("market_ticker")))
            reason = self._should_cancel_resting(tracked, row)
            if reason is not None:
                if self._cancel_order(tracked, reason=str(reason)):
                    changed = True

        # (2) build candidates
        cands = self._build_candidates(result)

        # (3) iterate candidates (throttled)
        submitted = 0
        for cand in cands:
            if submitted >= int(self.max_entries_per_tick):
                break

            pos = self.open_positions.get(cand.market_ticker)
            if pos is not None and str(pos.get("side")) and str(pos.get("side")) != str(cand.side):
                self._log_skip(cand, reason="position_side_conflict")
                continue

            current, target, size_reason = self._target_contracts_for_candidate(pos, cand)
            remaining = int(target - current)
            is_scale_in_attempt = current > 0

            if remaining <= 0:
                self._log_skip(
                    cand,
                    reason="target_reached",
                    extra={
                        "current_contracts": int(current),
                        "target_contracts": int(target),
                        "remaining_to_target": int(remaining),
                    },
                )
                continue

            if size_reason is not None:
                self._log_skip(
                    cand,
                    reason=str(size_reason),
                    extra={
                        "is_scale_in_attempt": bool(is_scale_in_attempt),
                        "current_contracts": int(current),
                        "target_contracts": int(target),
                        "remaining_to_target": int(remaining),
                    },
                )
                continue

            # No duplicate orders: at most one per (market, side)
            key = market_side_key(cand.market_ticker, cand.side)
            existing_oid = self.active_order_by_market.get(key)
            if existing_oid and existing_oid in self.open_orders:
                tracked = self.open_orders[existing_oid]
                if str(tracked.get("source")) == "maker" and str(tracked.get("status", "")).lower() == "resting":
                    cur_price = _as_int(tracked.get("price_cents"), 0)
                    cur_rem = _as_int(tracked.get("remaining_count"), _as_int(tracked.get("count"), 0))
                    if cur_price == int(cand.price_cents) and cur_rem == int(remaining):
                        self._log_skip(
                            cand,
                            reason="already_has_matching_resting_order",
                            extra={
                                "order_id": existing_oid,
                                "current_contracts": int(current),
                                "target_contracts": int(target),
                                "remaining_to_target": int(remaining),
                            },
                        )
                        continue

                    tracked["last_model_p"] = float(cand.p_yes)
                    tracked["last_edge_pp"] = float(cand.edge_pp)
                    if self._amend_order(tracked, new_price_cents=int(cand.price_cents), new_count=int(remaining)):
                        changed = True
                        submitted += 1
                        continue

                    if self._cancel_order(tracked, reason="amend_failed_replace"):
                        changed = True
                        existing_oid = None

                self._log_skip(
                    cand,
                    reason="already_has_active_order",
                    extra={
                        "order_id": existing_oid,
                        "current_contracts": int(current),
                        "target_contracts": int(target),
                        "remaining_to_target": int(remaining),
                    },
                )
                continue

            # caps (cost uses planned price + fee)
            add_cost = float(remaining) * ((float(cand.price_cents) + float(cand.fee_cents)) / 100.0)
            evt = cand.event_ticker
            is_new_market_in_event = int(_as_int(pos.get("total_count"), 0) if pos else 0) <= 0
            cap_reason = self._cap_check(
                event_ticker=evt,
                market_ticker=cand.market_ticker,
                add_cost_dollars=add_cost,
                is_new_market_in_event=is_new_market_in_event,
            )
            if cap_reason is not None:
                self._log_skip(
                    cand,
                    reason=cap_reason,
                    extra={
                        "is_scale_in_attempt": bool(is_scale_in_attempt),
                        "current_contracts": int(current),
                        "target_contracts": int(target),
                        "remaining_to_target": int(remaining),
                        "add_cost_dollars": float(add_cost),
                        "event_cost": float(self.event_cost.get(evt, 0.0)),
                        "market_cost": float(self.market_cost.get(cand.market_ticker, 0.0)),
                        "total_cost_all": float(self.total_cost_all),
                    },
                )
                continue

            if self._create_order_for_candidate(
                cand,
                remaining_to_target=int(remaining),
                current_contracts=int(current),
                target_contracts=int(target),
                is_scale_in_attempt=bool(is_scale_in_attempt),
            ):
                changed = True
                submitted += 1

        if changed:
            self._persist_state_file()

        print(
            f"[TRADE] tick event={result.event_ticker} markets={len(result.rows)} "
            f"candidates={len(cands)} entries={submitted} total_cost_all=${self.total_cost_all:.4f} "
            f"positions_all={self.positions_count_all} open_orders={len(self.open_orders)}"
        )

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "positions_count_all": int(self.positions_count_all),
            "total_cost_all": float(self.total_cost_all),
            "event_cost": self.event_cost,
            "event_positions": self.event_positions,
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

        try:
            from kalshi_edge.kalshi_api import get_positions
        except Exception as e:
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return

        cursor = None
        market_positions: List[dict] = []
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
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return

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
                "fills": [
                    {
                        "fill_id": "reconcile-" + str(uuid.uuid4()),
                        "ts_utc": utc_ts(),
                        "count": int(abs_pos),
                        "price_cents": 0,
                        "fee_cents": 0,
                        "cost_dollars": 0.0,
                        "p_yes": None,
                        "edge_pp": None,
                        "ev": None,
                        "source": "reconciled",
                        "order_id": None,
                    }
                ],
                "last_fill_ts_utc": utc_ts(),
                "last_fill_price_cents": 0,
                "last_fill_edge_pp": None,
                "strike": None,
                "subtitle": None,
                "implied_q_yes": None,
                "reconciled": True,
            }
            added += 1

        if added > 0:
            self._recompute_aggregates_from_positions()
            self._persist_state_file()

        self.log.log(
            "reconcile_done",
            {
                "event_ticker": event_ticker,
                "positions_seen": len(market_positions),
                "positions_added": int(added),
                "positions_count_all": int(self.positions_count_all),
                "total_cost_all": float(self.total_cost_all),
            },
        )


def debug_order_manager() -> None:
    """
    Tiny debug routine (no network) that simulates:
    - two ticks with the same market -> only one active order exists
    - a fill -> position count increases
    - a model p move beyond p_requote_pp -> resting order cancels
    """
    from dataclasses import dataclass

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
        sigma_blend: float = 0.0

    t = V2Trader(
        http=HttpClient(debug=False),
        auth=KalshiAuth(api_key_id="DUMMY", private_key_path="/dev/null"),
        kalshi_base_url="https://example.invalid",
        state_file=".debug_state_v22.json",
        trade_log_file="logs/debug_order_manager.jsonl",
        dry_run=True,
        order_mode="maker_only",
        post_only=True,
        max_contracts_per_market=2,
        allow_scale_in=True,
        p_requote_pp=0.02,
        cancel_stale_seconds=99999,
        max_entries_per_tick=1,
        fee_cents_maker=1,
        fee_cents_taker=1,
        ev_min=0.05,
    )

    row1 = _Row(
        ticker="TEST-MKT",
        p_model=0.70,
        strike=123.0,
        subtitle="debug",
        ob=_OB(ybid=50, nbid=50, ybuy=52, nbuy=52, spread_y=2, spread_n=2, yqty=10.0, nqty=10.0),
    )
    res1 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row1])  # type: ignore[arg-type]
    t.on_tick(res1)
    assert len(t.active_order_by_market) <= 1, "should have <=1 active order after tick 1"

    before = dict(t.active_order_by_market)
    t.on_tick(res1)
    assert t.active_order_by_market == before, "should not spam duplicate orders in watch mode"

    # Simulate a fill directly (dry-run resting orders don't auto-fill).
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
        ev=0.70 - (float(int(cand_price) + 1) / 100.0),
        source="maker",
        order_id="SIMULATED",
        strike=123.0,
        subtitle="debug",
        implied_q_yes=0.52,
    )
    assert t.open_positions["TEST-MKT"]["total_count"] == 1, "position should increase after fill"

    row2 = _Row(
        ticker="TEST-MKT",
        p_model=0.67,  # move by 0.03
        strike=123.0,
        subtitle="debug",
        ob=row1.ob,
    )
    res2 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row2])  # type: ignore[arg-type]
    t.on_tick(res2)
    assert len(t.active_order_by_market) <= 1, "should still have <=1 active order after requote"

    print("[DEBUG] order manager simulation OK")

"""
trader_v2.py

V2.2 trader: hold-to-expiration entry engine with resting-order support.

Key properties:
- Entry only (BUY): never submits exits.
- Multi-event state: can trade across discovered events (optional lock in run.py).
- Strategy iteration engine: maintains at most ONE active order per (market_ticker, side).
- Hybrid maker/taker: can place post-only resting orders when taker price isn't good enough.
- Scale-in is explicit via target sizing + caps; no duplicate order spam in watch mode.
"""

# from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from kalshi_edge.strategy_config import DEFAULT_CONFIG as _DEFAULT_CFG

EV_MIN = _DEFAULT_CFG.MIN_EV
ORDER_SIZE = _DEFAULT_CFG.ORDER_SIZE
MIN_TOP_SIZE = _DEFAULT_CFG.MIN_TOP_SIZE
SPREAD_MAX_CENTS = _DEFAULT_CFG.SPREAD_MAX_CENTS
MAX_POSITIONS_PER_EVENT = _DEFAULT_CFG.MAX_POSITIONS_PER_EVENT
MAX_COST_PER_EVENT = _DEFAULT_CFG.MAX_COST_PER_EVENT
MAX_COST_PER_STRIKE = _DEFAULT_CFG.MAX_COST_PER_MARKET
from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_auth import KalshiAuth
from kalshi_edge.order_manager import FillDelta, OrderManager, market_side_key, utc_ts
from kalshi_edge.pipeline import EvaluationResult
from kalshi_edge.trade_log import TradeLogger


SCHEMA = "v2.2"


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


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except Exception:
        return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # isoformat() with timezone
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _secs_since(ts: Optional[str]) -> Optional[float]:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())


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
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        # legacy-ish knobs (kept for compatibility)
        count: int = ORDER_SIZE,
        ev_min: float = EV_MIN,
        min_top_size: float = MIN_TOP_SIZE,
        spread_max_cents: Optional[int] = SPREAD_MAX_CENTS,
        max_positions_per_event: int = MAX_POSITIONS_PER_EVENT,
        max_cost_per_event: float = MAX_COST_PER_EVENT,
        dry_run: bool = False,
        # new caps
        max_cost_per_market: Optional[float] = None,
        max_total_cost: Optional[float] = None,
        max_total_positions: Optional[int] = None,
        # order behavior
        order_mode: str = "hybrid",  # taker_only | maker_only | hybrid
        post_only: bool = True,
        maker_time_in_force: str = "good_till_canceled",
        taker_time_in_force: str = "fill_or_kill",
        order_refresh_seconds: int = 10,
        cancel_stale_seconds: int = 60,
        p_requote_pp: float = 0.02,
        max_entries_per_tick: int = 1,
        max_contracts_per_market: int = 1,
        allow_scale_in: Optional[bool] = None,
        scale_in_cooldown_seconds: int = 60,
        min_edge_pp_entry: Optional[float] = None,
        min_edge_pp_scale_in: Optional[float] = None,
        maker_extra_buffer: float = 0.01,
        fee_cents_taker: int = 1,
        fee_cents_maker: int = 1,
        subaccount: Optional[int] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(trade_log_file)

        self.count = int(count)
        self.ev_min = float(ev_min)
        self.min_top_size = float(min_top_size)
        self.spread_max_cents = int(spread_max_cents) if spread_max_cents is not None else None

        self.max_positions_per_event = int(max_positions_per_event)
        self.max_cost_per_event = float(max_cost_per_event)
        self.max_cost_per_market = float(max_cost_per_market) if max_cost_per_market is not None else None
        self.max_total_cost = float(max_total_cost) if max_total_cost is not None else None
        self.max_total_positions = int(max_total_positions) if max_total_positions is not None else None

        self.order_mode = str(order_mode)
        self.post_only = bool(post_only)
        self.maker_time_in_force = str(maker_time_in_force)
        self.taker_time_in_force = str(taker_time_in_force)
        self.order_refresh_seconds = int(order_refresh_seconds)
        self.cancel_stale_seconds = int(cancel_stale_seconds)
        self.p_requote_pp = float(p_requote_pp)
        self.max_entries_per_tick = int(max_entries_per_tick)

        self.max_contracts_per_market = int(max_contracts_per_market)
        if allow_scale_in is None:
            allow_scale_in = self.max_contracts_per_market > 1
        self.allow_scale_in = bool(allow_scale_in)
        self.scale_in_cooldown_seconds = int(scale_in_cooldown_seconds)

        self.min_edge_pp_entry = float(min_edge_pp_entry) if min_edge_pp_entry is not None else float(self.ev_min)
        self.min_edge_pp_scale_in = (
            float(min_edge_pp_scale_in)
            if min_edge_pp_scale_in is not None
            else float(self.ev_min + 0.01)
        )
        self.maker_extra_buffer = float(maker_extra_buffer)

        self.fee_cents_taker = int(fee_cents_taker)
        self.fee_cents_maker = int(fee_cents_maker)
        self.dry_run = bool(dry_run)

        self.om = OrderManager(
            http=self.http,
            auth=self.auth,
            kalshi_base_url=self.kalshi_base_url,
            log=self.log,
            dry_run=self.dry_run,
            subaccount=subaccount,
        )

        # ---- state (v2.2) ----
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.market_cost: Dict[str, float] = {}
        self.event_cost: Dict[str, float] = {}
        self.event_positions: Dict[str, int] = {}
        self.total_cost_all: float = 0.0
        self.positions_count_all: int = 0

        self.open_orders: Dict[str, Dict[str, Any]] = {}
        self.active_order_by_market: Dict[str, str] = {}  # "TICKER|side" -> order_id

        self._load_from_state_file()

        self.log.log(
            "bot_start",
            {
                "schema": SCHEMA,
                "state_file": self.state_file,
                "trade_log_file": trade_log_file,
                "count": self.count,
                "ev_min": self.ev_min,
                "min_top_size": self.min_top_size,
                "spread_max_cents": self.spread_max_cents,
                "max_positions_per_event": self.max_positions_per_event,
                "max_cost_per_event": self.max_cost_per_event,
                "max_cost_per_market": self.max_cost_per_market,
                "max_total_cost": self.max_total_cost,
                "max_total_positions": self.max_total_positions,
                "order_mode": self.order_mode,
                "post_only": self.post_only,
                "maker_time_in_force": self.maker_time_in_force,
                "taker_time_in_force": self.taker_time_in_force,
                "order_refresh_seconds": self.order_refresh_seconds,
                "cancel_stale_seconds": self.cancel_stale_seconds,
                "p_requote_pp": self.p_requote_pp,
                "max_entries_per_tick": self.max_entries_per_tick,
                "max_contracts_per_market": self.max_contracts_per_market,
                "allow_scale_in": self.allow_scale_in,
                "scale_in_cooldown_seconds": self.scale_in_cooldown_seconds,
                "min_edge_pp_entry": self.min_edge_pp_entry,
                "min_edge_pp_scale_in": self.min_edge_pp_scale_in,
                "maker_extra_buffer": self.maker_extra_buffer,
                "fee_cents_taker": self.fee_cents_taker,
                "fee_cents_maker": self.fee_cents_maker,
                "dry_run": self.dry_run,
            },
        )

    # --------------------
    # state
    # --------------------

    def _persist_state_file(self) -> None:
        out = {
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
        }
        _write_state(self.state_file, out)

    def _recompute_aggregates_from_positions(self) -> None:
        self.market_cost = {}
        self.event_cost = {}
        self.event_positions = {}
        total_cost = 0.0

        for mkt, pos in self.open_positions.items():
            tc = _as_int(pos.get("total_count"), 0)
            if tc <= 0:
                continue
            evt = str(pos.get("event_ticker") or "")
            cost = _as_float(pos.get("total_cost_dollars"), 0.0)
            self.market_cost[str(mkt)] = float(cost)
            if evt:
                self.event_cost[evt] = float(self.event_cost.get(evt, 0.0) + cost)
                self.event_positions[evt] = int(self.event_positions.get(evt, 0) + 1)
            total_cost += cost

        self.total_cost_all = float(total_cost)
        self.positions_count_all = int(len([p for p in self.open_positions.values() if _as_int(p.get("total_count"), 0) > 0]))

    def _migrate_v2_to_v22(self, st: Dict[str, Any]) -> None:
        ops = st.get("open_positions")
        if not isinstance(ops, dict):
            return

        migrated_positions: Dict[str, Dict[str, Any]] = {}
        for market_ticker, old in ops.items():
            if not isinstance(market_ticker, str) or not isinstance(old, dict):
                continue
            event_ticker = str(old.get("event_ticker") or st.get("event_ticker") or "")
            side = str(old.get("side") or "")
            count = _as_int(old.get("count"), 0)
            entry_price_cents = old.get("entry_price_cents")
            price_cents = _as_int(entry_price_cents, 0) if isinstance(entry_price_cents, int) else 0
            entry_cost = _as_float(old.get("entry_cost_dollars"), 0.0)
            entry_fee = _as_float(old.get("entry_fee_dollars"), 0.0)
            p_yes = old.get("p_at_entry")
            edge_pp = old.get("edge_pp_at_entry")
            ev = old.get("ev_at_entry")

            fill_id = "migrate-" + str(uuid.uuid4())
            ts = str(old.get("entry_ts_utc") or utc_ts())

            fills = [
                {
                    "fill_id": fill_id,
                    "ts_utc": ts,
                    "count": int(count),
                    "price_cents": int(price_cents),
                    "fee_cents": int(round((entry_fee * 100.0) / float(count))) if count > 0 else None,
                    "cost_dollars": float(entry_cost),
                    "p_yes": float(p_yes) if isinstance(p_yes, (int, float)) else None,
                    "edge_pp": float(edge_pp) if isinstance(edge_pp, (int, float)) else None,
                    "ev": float(ev) if isinstance(ev, (int, float)) else None,
                    "source": "migrated",
                    "order_id": None,
                }
            ]

            migrated_positions[market_ticker] = {
                "market_ticker": market_ticker,
                "event_ticker": event_ticker,
                "side": side,
                "total_count": int(count),
                "total_cost_dollars": float(entry_cost),
                "total_fee_dollars": float(entry_fee),
                "fills": fills,
                "last_fill_ts_utc": ts,
                "last_fill_price_cents": int(price_cents) if price_cents else None,
                "last_fill_edge_pp": float(edge_pp) if isinstance(edge_pp, (int, float)) else None,
                "strike": old.get("strike"),
                "subtitle": old.get("subtitle"),
                "implied_q_yes": old.get("implied_q_yes"),
                "migrated_from": "v2",
            }

        self.open_positions = migrated_positions
        self.open_orders = {}
        self.active_order_by_market = {}
        self._recompute_aggregates_from_positions()
        self._persist_state_file()

    def _load_from_state_file(self) -> None:
        st = _read_state(self.state_file)
        if not isinstance(st, dict):
            return

        schema = st.get("schema")
        if schema == "v2":
            self._migrate_v2_to_v22(st)
            return

        if schema != SCHEMA:
            return

        ops = st.get("open_positions")
        if isinstance(ops, dict):
            self.open_positions = {
                str(k): v for k, v in ops.items() if isinstance(k, str) and isinstance(v, dict)
            }

        self.market_cost = {str(k): float(v) for k, v in (st.get("market_cost") or {}).items() if isinstance(k, str) and isinstance(v, (int, float))}
        self.event_cost = {str(k): float(v) for k, v in (st.get("event_cost") or {}).items() if isinstance(k, str) and isinstance(v, (int, float))}
        self.event_positions = {str(k): int(v) for k, v in (st.get("event_positions") or {}).items() if isinstance(k, str) and isinstance(v, int)}
        self.total_cost_all = _as_float(st.get("total_cost_all"), 0.0)
        self.positions_count_all = _as_int(st.get("positions_count_all"), 0)

        oos = st.get("open_orders")
        if isinstance(oos, dict):
            self.open_orders = {str(k): v for k, v in oos.items() if isinstance(k, str) and isinstance(v, dict)}

        aobm = st.get("active_order_by_market")
        if isinstance(aobm, dict):
            self.active_order_by_market = {str(k): str(v) for k, v in aobm.items() if isinstance(k, str) and isinstance(v, str)}

        # best-effort consistency: if state older, recompute aggregates
        if not self.market_cost or not self.event_cost:
            self._recompute_aggregates_from_positions()

    # --------------------
    # caps + sizing
    # --------------------

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

        if self.max_cost_per_market is not None:
            if (float(self.market_cost.get(market_ticker, 0.0)) + float(add_cost_dollars)) > float(self.max_cost_per_market):
                return "max_cost_per_market"

        if self.max_total_cost is not None:
            if (float(self.total_cost_all) + float(add_cost_dollars)) > float(self.max_total_cost):
                return "max_total_cost"

        if self.max_total_positions is not None:
            # total distinct markets with position >0
            new_pos_count = int(self.positions_count_all + (1 if is_new_market_in_event else 0))
            if new_pos_count > int(self.max_total_positions):
                return "max_total_positions"

        return None

    def _cooldown_ok(self, pos: Optional[Dict[str, Any]]) -> bool:
        if pos is None:
            return True
        last_ts = pos.get("last_fill_ts_utc")
        if not isinstance(last_ts, str) or not last_ts:
            return True
        secs = _secs_since(last_ts)
        if secs is None:
            return True
        return secs >= float(self.scale_in_cooldown_seconds)

    def _target_contracts_for_candidate(self, pos: Optional[Dict[str, Any]], cand: _ActionCandidate) -> Tuple[int, int, Optional[str]]:
        current = _as_int(pos.get("total_count"), 0) if pos else 0
        if current <= 0:
            if cand.edge_pp < float(self.min_edge_pp_entry):
                return current, current, "edge_below_min_entry"
            target = min(int(self.max_contracts_per_market), int(current + self.count))
            return current, int(target), None

        # scale-in attempt
        if not self.allow_scale_in:
            return current, current, "scale_in_disabled"
        if cand.edge_pp < float(self.min_edge_pp_scale_in):
            return current, current, "edge_below_min_scale_in"
        if not self._cooldown_ok(pos):
            return current, current, "scale_in_cooldown"

        target = min(int(self.max_contracts_per_market), int(current + self.count))
        return current, int(target), None

    # --------------------
    # logging
    # --------------------

    def _log_skip(self, cand: _ActionCandidate, *, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "event_ticker": cand.event_ticker,
            "market_ticker": cand.market_ticker,
            "side": cand.side,
            "source": cand.source,
            "price_cents": int(cand.price_cents),
            "fee_cents": int(cand.fee_cents),
            "p_yes": float(cand.p_yes),
            "implied_q_yes": float(cand.implied_q_yes) if cand.implied_q_yes is not None else None,
            "edge_pp": float(cand.edge_pp),
            "ev": float(cand.ev),
            "max_price_cents": int(cand.max_price_cents),
            "bid": int(cand.bid_cents) if cand.bid_cents is not None else None,
            "ask_proxy": int(cand.ask_proxy_cents) if cand.ask_proxy_cents is not None else None,
            "spread_cents": int(cand.spread_cents) if cand.spread_cents is not None else None,
            "top_size": float(cand.top_size) if cand.top_size is not None else None,
            "skip_reason": str(reason),
            "dry_run": self.dry_run,
        }
        if extra:
            payload.update(extra)
        self.log.log("skip", payload)

    # --------------------
    # fills -> positions
    # --------------------

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
        ev: float,
        source: str,
        order_id: str,
        strike: Optional[float],
        subtitle: Optional[str],
        implied_q_yes: Optional[float],
        ts_utc: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Returns (is_scale_in, position_dict_after).
        """
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
            "ev": float(ev),
            "source": str(source),
            "order_id": str(order_id),
        }

        if pos is None:
            pos = {
                "market_ticker": str(market_ticker),
                "event_ticker": str(event_ticker),
                "side": str(side),
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

        # Side consistency guard: do not allow "yes"+"no" in same market.
        if str(pos.get("side")) and str(pos.get("side")) != str(side):
            self.log.log(
                "fill_side_conflict",
                {
                    "market_ticker": market_ticker,
                    "existing_side": pos.get("side"),
                    "fill_side": side,
                    "order_id": order_id,
                },
            )
            return was_open, pos

        pos["event_ticker"] = str(event_ticker)
        pos["side"] = str(side)
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

        # aggregates
        self.market_cost[market_ticker] = float(self.market_cost.get(market_ticker, 0.0) + cost_dollars)
        self.event_cost[event_ticker] = float(self.event_cost.get(event_ticker, 0.0) + cost_dollars)
        self.total_cost_all = float(self.total_cost_all + cost_dollars)

        if not was_open:
            self.event_positions[event_ticker] = int(self.event_positions.get(event_ticker, 0) + 1)
            self.positions_count_all = int(self.positions_count_all + 1)

        return was_open, pos

    # --------------------
    # candidate building
    # --------------------

    def _liquidity_check(self, spread_cents: Optional[int], top_size: Optional[float]) -> Optional[str]:
        if self.spread_max_cents is not None and spread_cents is not None and int(spread_cents) > int(self.spread_max_cents):
            return "spread_too_wide"
        if top_size is not None and float(top_size) < float(self.min_top_size):
            return "top_size_too_small"
        return None

    def _max_acceptable_price_cents(self, *, p_win: float, fee_buffer_cents: int) -> int:
        # floor(100*(p_win - ev_min)) - fee
        x = int(math.floor(100.0 * (float(p_win) - float(self.ev_min)))) - int(fee_buffer_cents)
        return _clamp_int(x, 0, 99)

    def _edge_at_price(self, *, p_win: float, price_cents: int, fee_cents: int) -> float:
        return float(p_win) - (float(price_cents + fee_cents) / 100.0)

    def _best_action_for_side(
        self,
        row,
        *,
        event_ticker: str,
        side: str,
        existing_side: Optional[str],
    ) -> Optional[_ActionCandidate]:
        # If we already hold the other side in this market, skip.
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

        # TAKE NOW (taker)
        if self.order_mode in {"taker_only", "hybrid"} and ask_proxy is not None:
            max_price = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_taker)
            liq_reason = self._liquidity_check(spread, top_size)
            if liq_reason is None and int(ask_proxy) <= int(max_price):
                ev_take = self._edge_at_price(p_win=p_win, price_cents=int(ask_proxy), fee_cents=self.fee_cents_taker)
                best = _ActionCandidate(
                    market_ticker=str(row.ticker),
                    event_ticker=str(event_ticker),
                    side=str(side),
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

        # MAKE (resting maker)
        if self.order_mode in {"maker_only", "hybrid"} and bid is not None:
            max_price_maker = self._max_acceptable_price_cents(p_win=p_win, fee_buffer_cents=self.fee_cents_maker)
            if int(max_price_maker) > int(bid):
                if self.post_only and ask_proxy is None:
                    # Without an ask-proxy we can't confidently avoid crossing in post-only mode.
                    return best
                desired_bid = min(int(max_price_maker), int(bid) + 1)
                if self.post_only and ask_proxy is not None and int(desired_bid) >= int(ask_proxy):
                    return best

                ev_make = self._edge_at_price(p_win=p_win, price_cents=int(desired_bid), fee_cents=self.fee_cents_maker)
                if ev_make >= float(self.ev_min + self.maker_extra_buffer):
                    cand_make = _ActionCandidate(
                        market_ticker=str(row.ticker),
                        event_ticker=str(event_ticker),
                        side=str(side),
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
            existing_side = str(existing.get("side")) if isinstance(existing, dict) and existing.get("side") in {"yes", "no"} else None

            c_yes = self._best_action_for_side(
                row, event_ticker=str(result.event_ticker), side="yes", existing_side=existing_side
            )
            c_no = self._best_action_for_side(
                row, event_ticker=str(result.event_ticker), side="no", existing_side=existing_side
            )

            best = None
            if c_yes is not None:
                best = c_yes
            if c_no is not None and (best is None or c_no.ev > best.ev):
                best = c_no

            if best is not None:
                out.append(best)

        out.sort(key=lambda c: c.ev, reverse=True)
        return out

    # --------------------
    # order management
    # --------------------

    def _cleanup_order_refs(self, order_id: str) -> None:
        # Remove from market map if present
        to_del = [k for k, v in self.active_order_by_market.items() if v == order_id]
        for k in to_del:
            self.active_order_by_market.pop(k, None)
        self.open_orders.pop(order_id, None)

    def refresh_orders_and_apply_fills(self, result: EvaluationResult) -> bool:
        """
        Returns True if state changed (fills detected or cleanup).
        """
        changed = False
        by_ticker = {str(r.ticker): r for r in result.rows}

        for order_id in list(self.open_orders.keys()):
            tracked = self.open_orders.get(order_id)
            if not isinstance(tracked, dict):
                self.open_orders.pop(order_id, None)
                changed = True
                continue

            status = str(tracked.get("status", "")).lower()
            if _is_terminal(status):
                self._cleanup_order_refs(order_id)
                changed = True
                continue

            secs = _secs_since(tracked.get("last_checked_ts_utc"))
            if secs is not None and secs < float(self.order_refresh_seconds):
                continue

            try:
                tracked2, delta = self.om.refresh_tracked_order(tracked)
            except Exception as e:
                self.log.log("order_refresh_failed", {"order_id": order_id, "error": str(e)})
                continue

            self.open_orders[order_id] = tracked2

            if delta is not None and delta.delta_fill_count > 0:
                row = by_ticker.get(str(tracked2.get("market_ticker")))
                p_yes = float(row.p_model) if row is not None else float(tracked2.get("last_model_p") or 0.5)
                side = str(tracked2.get("side"))
                p_win = p_yes if side == "yes" else (1.0 - p_yes)
                price_cents = delta.avg_price_cents or _as_int(tracked2.get("price_cents"), 0)
                fee_cents = delta.avg_fee_cents or (
                    int(self.fee_cents_maker) if str(tracked2.get("source")) == "maker" else int(self.fee_cents_taker)
                )
                edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))

                pos_before = self.open_positions.get(str(tracked2.get("market_ticker")))
                was_open = pos_before is not None and _as_int(pos_before.get("total_count"), 0) > 0

                is_scale_in, pos_after = self._apply_fill(
                    market_ticker=str(tracked2.get("market_ticker")),
                    event_ticker=str(tracked2.get("event_ticker")),
                    side=side,
                    fill_count=int(delta.delta_fill_count),
                    price_cents=int(price_cents),
                    fee_cents=int(fee_cents),
                    p_yes=float(p_yes),
                    edge_pp=float(edge_pp),
                    ev=float(edge_pp),
                    source=str(tracked2.get("source")),
                    order_id=str(tracked2.get("order_id")),
                    strike=float(row.strike) if row is not None else None,
                    subtitle=str(row.subtitle) if row is not None else None,
                    implied_q_yes=(float(row.ob.ybuy) / 100.0) if (row is not None and row.ob.ybuy is not None) else None,
                    ts_utc=delta.ts_utc,
                )

                self.log.log(
                    "fill_detected",
                    {
                        "order_id": str(tracked2.get("order_id")),
                        "market_ticker": str(tracked2.get("market_ticker")),
                        "event_ticker": str(tracked2.get("event_ticker")),
                        "side": side,
                        "source": str(tracked2.get("source")),
                        "delta_fill_count": int(delta.delta_fill_count),
                        "delta_cost_cents": int(delta.delta_cost_cents),
                        "delta_fee_cents": int(delta.delta_fee_cents),
                        "avg_price_cents": int(price_cents),
                        "avg_fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "was_open": bool(was_open),
                        "is_scale_in": bool(is_scale_in),
                        "position_total_count": int(_as_int(pos_after.get("total_count"), 0)),
                    },
                )

                self.log.log(
                    "scale_in_filled" if is_scale_in else "entry_filled",
                    {
                        "order_id": str(tracked2.get("order_id")),
                        "market_ticker": str(tracked2.get("market_ticker")),
                        "event_ticker": str(tracked2.get("event_ticker")),
                        "side": side,
                        "count": int(delta.delta_fill_count),
                        "price_cents": int(price_cents),
                        "fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "ev": float(edge_pp),
                        "dry_run": self.dry_run,
                    },
                )

                changed = True

            status2 = str(tracked2.get("status", "")).lower()
            if _is_terminal(status2) or _as_int(tracked2.get("remaining_count"), 0) <= 0:
                self._cleanup_order_refs(order_id)
                changed = True

        return changed

    def _should_cancel_resting(self, tracked: Dict[str, Any], row) -> Optional[str]:
        status = str(tracked.get("status", "")).lower()
        if status != "resting":
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
        fee_cents = int(self.fee_cents_maker) if str(tracked.get("source")) == "maker" else int(self.fee_cents_taker)
        edge_now = self._edge_at_price(p_win=p_win, price_cents=price_cents, fee_cents=fee_cents)
        if edge_now < float(self.ev_min + self.maker_extra_buffer):
            return "maker_edge_too_low_now"

        return None

    def _cancel_order(self, tracked: Dict[str, Any], *, reason: str) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log(
            "order_cancel_submit",
            {
                "order_id": oid,
                "market_ticker": tracked.get("market_ticker"),
                "event_ticker": tracked.get("event_ticker"),
                "side": tracked.get("side"),
                "reason": str(reason),
                "dry_run": self.dry_run,
            },
        )
        try:
            resp = self.om.submit_cancel(tracked)
            self.log.log("order_canceled", {"order_id": oid, "resp": resp, "dry_run": self.dry_run})
        except Exception as e:
            self.log.log("order_cancel_failed", {"order_id": oid, "error": str(e), "dry_run": self.dry_run})
            return False

        # cleanup locally regardless; next refresh can reconcile if needed.
        self._cleanup_order_refs(oid)
        return True

    def _amend_order(self, tracked: Dict[str, Any], *, new_price_cents: int, new_count: int) -> bool:
        oid = str(tracked.get("order_id"))
        self.log.log(
            "order_amend_submit",
            {
                "order_id": oid,
                "market_ticker": tracked.get("market_ticker"),
                "event_ticker": tracked.get("event_ticker"),
                "side": tracked.get("side"),
                "new_price_cents": int(new_price_cents),
                "new_count": int(new_count),
                "dry_run": self.dry_run,
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
        current_contracts: int,
        target_contracts: int,
        is_scale_in_attempt: bool,
    ) -> bool:
        tif = self.taker_time_in_force if cand.source == "taker" else self.maker_time_in_force
        key = market_side_key(cand.market_ticker, cand.side)

        self.log.log(
            "order_submit",
            {
                "mode": cand.source,
                "post_only": bool(self.post_only if cand.source == "maker" else False),
                "time_in_force": str(tif),
                "event_ticker": cand.event_ticker,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(remaining_to_target),
                "price_cents": int(cand.price_cents),
                "fee_cents": int(cand.fee_cents),
                "max_price_cents": int(cand.max_price_cents),
                "bid": int(cand.bid_cents) if cand.bid_cents is not None else None,
                "ask_proxy": int(cand.ask_proxy_cents) if cand.ask_proxy_cents is not None else None,
                "p_yes": float(cand.p_yes),
                "edge_pp": float(cand.edge_pp),
                "ev": float(cand.ev),
                "is_scale_in_attempt": bool(is_scale_in_attempt),
                "current_contracts": int(current_contracts),
                "target_contracts": int(target_contracts),
                "remaining_to_target": int(remaining_to_target),
                "dry_run": self.dry_run,
            },
        )

        tracked, resp = self.om.submit_new_order(
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
        )

        oid = str(tracked.get("order_id"))
        self.open_orders[oid] = tracked
        self.active_order_by_market[key] = oid

        status = str(tracked.get("status", "")).lower()
        if status == "resting":
            self.log.log("order_resting", {"order_id": oid, "market_ticker": cand.market_ticker, "side": cand.side, "dry_run": self.dry_run})

        # If it executed immediately, apply fills immediately (don’t wait for refresh cadence).
        if not self.dry_run:
            fc = _as_int(tracked.get("fill_count"), 0)
            if fc > 0:
                total_cost = _as_int(tracked.get("last_fill_cost_cents"), 0)
                total_fee = _as_int(tracked.get("last_fee_paid_cents"), 0)
                delta = FillDelta(
                    delta_fill_count=int(fc),
                    delta_cost_cents=int(total_cost),
                    delta_fee_cents=int(total_fee),
                    ts_utc=utc_ts(),
                )

                price_cents = delta.avg_price_cents or int(cand.price_cents)
                fee_cents = delta.avg_fee_cents or int(cand.fee_cents)
                p_yes = float(cand.p_yes)
                p_win = p_yes if cand.side == "yes" else (1.0 - p_yes)
                edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(price_cents), fee_cents=int(fee_cents))

                is_scale_in, pos_after = self._apply_fill(
                    market_ticker=cand.market_ticker,
                    event_ticker=cand.event_ticker,
                    side=cand.side,
                    fill_count=int(delta.delta_fill_count),
                    price_cents=int(price_cents),
                    fee_cents=int(fee_cents),
                    p_yes=float(p_yes),
                    edge_pp=float(edge_pp),
                    ev=float(edge_pp),
                    source=str(cand.source),
                    order_id=str(oid),
                    strike=float(cand.strike),
                    subtitle=str(cand.subtitle),
                    implied_q_yes=cand.implied_q_yes,
                    ts_utc=delta.ts_utc,
                )

                self.log.log(
                    "fill_detected",
                    {
                        "order_id": oid,
                        "market_ticker": cand.market_ticker,
                        "event_ticker": cand.event_ticker,
                        "side": cand.side,
                        "source": cand.source,
                        "delta_fill_count": int(delta.delta_fill_count),
                        "delta_cost_cents": int(delta.delta_cost_cents),
                        "delta_fee_cents": int(delta.delta_fee_cents),
                        "avg_price_cents": int(price_cents),
                        "avg_fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "is_scale_in": bool(is_scale_in),
                        "position_total_count": int(_as_int(pos_after.get("total_count"), 0)),
                    },
                )

                self.log.log(
                    "scale_in_filled" if is_scale_in else "entry_filled",
                    {
                        "order_id": oid,
                        "market_ticker": cand.market_ticker,
                        "event_ticker": cand.event_ticker,
                        "side": cand.side,
                        "count": int(delta.delta_fill_count),
                        "price_cents": int(price_cents),
                        "fee_cents": int(fee_cents),
                        "p_yes": float(p_yes),
                        "edge_pp": float(edge_pp),
                        "ev": float(edge_pp),
                        "dry_run": False,
                    },
                )

                # If fully executed, clear order tracking immediately.
                if _as_int(tracked.get("remaining_count"), 0) <= 0 or _is_terminal(status):
                    self._cleanup_order_refs(oid)

        # If it executed immediately (taker), rely on refresh loop to apply fill;
        # but in dry_run submit_new_order marks executed and fill_count, so a refresh isn't needed.
        if self.dry_run and status in {"executed", "filled"}:
            fake_delta = FillDelta(
                delta_fill_count=int(remaining_to_target),
                delta_cost_cents=int(remaining_to_target * int(cand.price_cents)),
                delta_fee_cents=int(remaining_to_target * int(cand.fee_cents)),
                ts_utc=utc_ts(),
            )
            tracked["fill_count"] = int(remaining_to_target)
            tracked["remaining_count"] = 0
            tracked["status"] = "executed"
            self.open_orders[oid] = tracked
            # Apply immediately so dry-run keeps "entry becomes position" semantics.
            p_now = float(cand.p_yes)
            p_win = p_now if cand.side == "yes" else (1.0 - p_now)
            edge_pp = self._edge_at_price(p_win=p_win, price_cents=int(cand.price_cents), fee_cents=int(cand.fee_cents))
            is_scale_in, _ = self._apply_fill(
                market_ticker=cand.market_ticker,
                event_ticker=cand.event_ticker,
                side=cand.side,
                fill_count=int(fake_delta.delta_fill_count),
                price_cents=int(cand.price_cents),
                fee_cents=int(cand.fee_cents),
                p_yes=float(p_now),
                edge_pp=float(edge_pp),
                ev=float(edge_pp),
                source=str(cand.source),
                order_id=str(oid),
                strike=float(cand.strike),
                subtitle=str(cand.subtitle),
                implied_q_yes=cand.implied_q_yes,
                ts_utc=fake_delta.ts_utc,
            )
            self.log.log("scale_in_filled" if is_scale_in else "entry_filled", {"order_id": oid, "market_ticker": cand.market_ticker, "side": cand.side, "count": int(remaining_to_target), "dry_run": True})
            self._cleanup_order_refs(oid)

        return True

    # --------------------
    # main tick
    # --------------------

    def on_tick(self, result: EvaluationResult) -> None:
        """
        Tick loop (v2.2):
        1) refresh_orders_and_apply_fills(result)
        2) build candidate actions from rows (maker/taker) and sort by EV desc
        3) iterate candidates up to max_entries_per_tick:
           - apply caps and target sizing
           - create/manage order for that (market, side)
        4) persist state if anything changed
        """
        changed = False

        # (1) refresh and apply fills
        if self.refresh_orders_and_apply_fills(result):
            changed = True

        # cancel/adverse-selection guard for active resting maker orders
        by_ticker = {str(r.ticker): r for r in result.rows}
        for key, oid in list(self.active_order_by_market.items()):
            tracked = self.open_orders.get(oid)
            if not isinstance(tracked, dict):
                self.active_order_by_market.pop(key, None)
                changed = True
                continue
            if str(tracked.get("source")) != "maker":
                continue
            row = by_ticker.get(str(tracked.get("market_ticker")))
            reason = self._should_cancel_resting(tracked, row)
            if reason is not None:
                if self._cancel_order(tracked, reason=str(reason)):
                    changed = True

        # (2) build candidates
        cands = self._build_candidates(result)

        # (3) iterate candidates (throttled)
        submitted = 0
        for cand in cands:
            if submitted >= int(self.max_entries_per_tick):
                break

            pos = self.open_positions.get(cand.market_ticker)
            if pos is not None and str(pos.get("side")) and str(pos.get("side")) != str(cand.side):
                self._log_skip(cand, reason="position_side_conflict")
                continue

            current, target, size_reason = self._target_contracts_for_candidate(pos, cand)
            remaining = int(target - current)
            is_scale_in_attempt = current > 0

            if remaining <= 0:
                self._log_skip(
                    cand,
                    reason="target_reached",
                    extra={"current_contracts": int(current), "target_contracts": int(target), "remaining_to_target": int(remaining)},
                )
                continue

            if size_reason is not None:
                self._log_skip(
                    cand,
                    reason=str(size_reason),
                    extra={"is_scale_in_attempt": bool(is_scale_in_attempt), "current_contracts": int(current), "target_contracts": int(target), "remaining_to_target": int(remaining)},
                )
                continue

            # No duplicate orders: at most one per (market, side)
            key = market_side_key(cand.market_ticker, cand.side)
            existing_oid = self.active_order_by_market.get(key)
            if existing_oid and existing_oid in self.open_orders:
                tracked = self.open_orders[existing_oid]
                # For resting maker orders: amend price/count (prefer amend over cancel+replace)
                if str(tracked.get("source")) == "maker" and str(tracked.get("status", "")).lower() == "resting":
                    cur_price = _as_int(tracked.get("price_cents"), 0)
                    cur_rem = _as_int(tracked.get("remaining_count"), _as_int(tracked.get("count"), 0))
                    if cur_price == int(cand.price_cents) and cur_rem == int(remaining):
                        self._log_skip(
                            cand,
                            reason="already_has_matching_resting_order",
                            extra={"order_id": existing_oid, "current_contracts": int(current), "target_contracts": int(target), "remaining_to_target": int(remaining)},
                        )
                        continue
                    tracked["last_model_p"] = float(cand.p_yes)
                    tracked["last_edge_pp"] = float(cand.edge_pp)
                    if self._amend_order(tracked, new_price_cents=int(cand.price_cents), new_count=int(remaining)):
                        changed = True
                        submitted += 1
                        continue

                    # Amend failed; fall back to cancel+replace
                    if self._cancel_order(tracked, reason="amend_failed_replace"):
                        changed = True
                        existing_oid = None

                # Otherwise: don't create duplicates; wait for refresh/terminal.
                self._log_skip(
                    cand,
                    reason="already_has_active_order",
                    extra={"order_id": existing_oid, "current_contracts": int(current), "target_contracts": int(target), "remaining_to_target": int(remaining)},
                )
                continue

            # caps (cost uses planned price + fee)
            add_cost = float(remaining) * ((float(cand.price_cents) + float(cand.fee_cents)) / 100.0)
            evt = cand.event_ticker
            is_new_market_in_event = int(_as_int(pos.get("total_count"), 0) if pos else 0) <= 0
            cap_reason = self._cap_check(
                event_ticker=evt,
                market_ticker=cand.market_ticker,
                add_cost_dollars=add_cost,
                is_new_market_in_event=is_new_market_in_event,
            )
            if cap_reason is not None:
                self._log_skip(
                    cand,
                    reason=cap_reason,
                    extra={
                        "is_scale_in_attempt": bool(is_scale_in_attempt),
                        "current_contracts": int(current),
                        "target_contracts": int(target),
                        "remaining_to_target": int(remaining),
                        "add_cost_dollars": float(add_cost),
                        "event_cost": float(self.event_cost.get(evt, 0.0)),
                        "market_cost": float(self.market_cost.get(cand.market_ticker, 0.0)),
                        "total_cost_all": float(self.total_cost_all),
                    },
                )
                continue

            # submit new order
            if self._create_order_for_candidate(
                cand,
                remaining_to_target=int(remaining),
                current_contracts=int(current),
                target_contracts=int(target),
                is_scale_in_attempt=bool(is_scale_in_attempt),
            ):
                changed = True
                submitted += 1

        # (4) persist state
        if changed:
            self._persist_state_file()

        print(
            f"[TRADE] tick event={result.event_ticker} markets={len(result.rows)} "
            f"candidates={len(cands)} entries={submitted} total_cost_all=${self.total_cost_all:.4f} "
            f"positions_all={self.positions_count_all} open_orders={len(self.open_orders)}"
        )

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "positions_count_all": int(self.positions_count_all),
            "total_cost_all": float(self.total_cost_all),
            "event_cost": self.event_cost,
            "event_positions": self.event_positions,
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "active_order_by_market": self.active_order_by_market,
        }

    def on_shutdown(self, last_result: Optional[EvaluationResult] = None) -> None:
        snap = self.snapshot_state()
        if last_result is not None:
            snap.update({"spot": float(last_result.market_state.spot), "minutes_left": float(last_result.minutes_left)})
        self.log.log("bot_shutdown", snap)

    # --------------------
    # optional reconcile
    # --------------------

    def reconcile_state(self, event_ticker: str) -> None:
        """
        Best-effort reconcile against Kalshi positions.
        This is intentionally conservative: it only *adds* missing positions and does not
        attempt to infer historical entry prices/cost.
        """
        event_ticker = str(event_ticker).upper()

        if self.dry_run:
            self.log.log("reconcile_skipped", {"event_ticker": event_ticker, "reason": "dry_run"})
            return

        try:
            from kalshi_edge.kalshi_api import get_positions
        except Exception as e:
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return

        cursor = None
        market_positions: List[dict] = []
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
            self.log.log("reconcile_failed", {"event_ticker": event_ticker, "error": str(e)})
            return

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

            # Unknown entry prices; record a synthetic fill at price=0, fee=0.
            self.open_positions[tkr] = {
                "market_ticker": tkr,
                "event_ticker": event_ticker,
                "side": side,
                "total_count": int(abs_pos),
                "total_cost_dollars": 0.0,
                "total_fee_dollars": 0.0,
                "fills": [
                    {
                        "fill_id": "reconcile-" + str(uuid.uuid4()),
                        "ts_utc": utc_ts(),
                        "count": int(abs_pos),
                        "price_cents": 0,
                        "fee_cents": 0,
                        "cost_dollars": 0.0,
                        "p_yes": None,
                        "edge_pp": None,
                        "ev": None,
                        "source": "reconciled",
                        "order_id": None,
                    }
                ],
                "last_fill_ts_utc": utc_ts(),
                "last_fill_price_cents": 0,
                "last_fill_edge_pp": None,
                "strike": None,
                "subtitle": None,
                "implied_q_yes": None,
                "reconciled": True,
            }
            added += 1

        if added > 0:
            self._recompute_aggregates_from_positions()
            self._persist_state_file()

        self.log.log(
            "reconcile_done",
            {
                "event_ticker": event_ticker,
                "positions_seen": len(market_positions),
                "positions_added": int(added),
                "positions_count_all": int(self.positions_count_all),
                "total_cost_all": float(self.total_cost_all),
            },
        )


def debug_order_manager() -> None:
    """
    Tiny debug routine (no network) that simulates:
    - two ticks with the same market -> only one active order exists
    - a fill -> position count increases and remaining_to_target decreases
    - a model p move beyond p_requote_pp -> resting order cancels
    """
    from dataclasses import dataclass

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
        event_ticker: str
        p_model: float
        strike: float
        subtitle: str
        ob: _OB

    @dataclass
    class _MS:
        spot: float = 0.0
        sigma_blend: float = 0.0

    # Make a V2Trader in dry-run mode (no HTTP calls) with maker-only so we get resting orders.
    t = V2Trader(
        http=HttpClient(debug=False),
        auth=KalshiAuth(api_key_id="DUMMY", private_key_path="/dev/null"),  # not used in dry-run
        kalshi_base_url="https://example.invalid",
        state_file=".debug_state_v22.json",
        trade_log_file="logs/debug_order_manager.jsonl",
        dry_run=True,
        order_mode="maker_only",
        post_only=True,
        max_contracts_per_market=2,
        allow_scale_in=True,
        p_requote_pp=0.02,
        cancel_stale_seconds=99999,
        max_entries_per_tick=1,
        fee_cents_maker=1,
        fee_cents_taker=1,
        ev_min=0.05,
    )

    # Tick 1: create one resting order
    row1 = _Row(
        ticker="TEST-MKT",
        event_ticker="TEST-EVT",
        p_model=0.70,
        strike=123.0,
        subtitle="debug",
        ob=_OB(ybid=50, nbid=50, ybuy=52, nbuy=52, spread_y=2, spread_n=2, yqty=10.0, nqty=10.0),
    )
    res1 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row1])  # type: ignore[arg-type]
    t.on_tick(res1)
    assert len(t.active_order_by_market) <= 1, "should have <=1 active order after tick 1"

    # Tick 2 (same inputs): should NOT create a second order
    before = dict(t.active_order_by_market)
    t.on_tick(res1)
    assert t.active_order_by_market == before, "should not spam duplicate orders in watch mode"

    # Simulate a fill by directly applying fill to position (dry-run resting orders do not auto-fill).
    # This simulates the refresh+fill detection path at a high level.
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
        ev=0.70 - (float(int(cand_price) + 1) / 100.0),
        source="maker",
        order_id="SIMULATED",
        strike=123.0,
        subtitle="debug",
        implied_q_yes=0.52,
    )
    assert t.open_positions["TEST-MKT"]["total_count"] == 1, "position should increase after fill"

    # p move beyond p_requote_pp should cancel resting order on next tick
    row2 = _Row(
        ticker="TEST-MKT",
        event_ticker="TEST-EVT",
        p_model=0.67,  # move by 0.03
        strike=123.0,
        subtitle="debug",
        ob=row1.ob,
    )
    res2 = EvaluationResult(event_ticker="TEST-EVT", event_title="debug", minutes_left=10.0, market_state=_MS(), rows=[row2])  # type: ignore[arg-type]
    t.on_tick(res2)
    # The cancel happens, then candidate loop may create a new order; but we still must have <=1.
    assert len(t.active_order_by_market) <= 1, "should still have <=1 active order after requote"

    print("[DEBUG] order manager simulation OK")


def _is_filled(resp: Dict[str, Any], want_count: int) -> bool:
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    if order is None:
        return False
    status = str(order.get("status", "")).lower()
    fill_count = order.get("fill_count")
    if isinstance(fill_count, int) and fill_count >= int(want_count):
        return True
    return status in {"executed", "filled"}


@dataclass
class _Candidate:
    market_ticker: str
    event_ticker: str
    side: str  # "yes" or "no"
    count: int
    price_cents: int
    fee_cents: int
    p_yes: float
    strike: float
    subtitle: str
    implied_q_yes: Optional[float]
    edge_pp: float
    ev: float
    spread_cents: Optional[int]
    top_size: Optional[float]

    @property
    def fee_dollars(self) -> float:
        return float(self.fee_cents) / 100.0

    @property
    def price_dollars(self) -> float:
        return float(self.price_cents) / 100.0

    @property
    def total_cost_dollars(self) -> float:
        return float(self.count) * (self.price_dollars + self.fee_dollars)

    @property
    def total_fee_dollars(self) -> float:
        return float(self.count) * self.fee_dollars

    @property
    def implied_q(self) -> Optional[float]:
        """
        Market-implied probability of YES from the executable entry price.
        YES buy price is implied P(YES); NO buy price is implied P(NO) so implied P(YES)=1-price.
        """
        q = self.price_dollars
        if self.side == "yes":
            return q
        return 1.0 - q


class V2Trader:
    def __init__(
        self,
        *,
        http: HttpClient,
        auth: KalshiAuth,
        kalshi_base_url: str,
        state_file: str,
        trade_log_file: str = "trade_log.jsonl",
        fee_cents: int = 1,
        count: int = ORDER_SIZE,
        ev_min: float = EV_MIN,
        min_top_size: float = MIN_TOP_SIZE,
        spread_max_cents: Optional[int] = SPREAD_MAX_CENTS,
        max_positions_per_event: int = MAX_POSITIONS_PER_EVENT,
        max_cost_per_event: float = MAX_COST_PER_EVENT,
        max_cost_per_strike: float = MAX_COST_PER_STRIKE,
        dry_run: bool = False,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.state_file = state_file
        self.log = TradeLogger(trade_log_file)

        self.fee_cents = int(fee_cents)
        self.count = int(count)
        self.ev_min = float(ev_min)
        self.min_top_size = float(min_top_size)
        self.spread_max_cents = int(spread_max_cents) if spread_max_cents is not None else None
        self.max_positions_per_event = int(max_positions_per_event)
        self.max_cost_per_event = float(max_cost_per_event)
        self.max_cost_per_strike = float(max_cost_per_strike)
        self.dry_run = bool(dry_run)

        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.entered_markets: set[str] = set()
        self.total_cost_event: float = 0.0
        self.positions_count: int = 0
        self.event_ticker: Optional[str] = None

        self._load_from_state_file()

        self.log.log(
            "bot_start",
            {
                "state_file": self.state_file,
                "trade_log_file": trade_log_file,
                "fee_cents": self.fee_cents,
                "count": self.count,
                "ev_min": self.ev_min,
                "min_top_size": self.min_top_size,
                "spread_max_cents": self.spread_max_cents,
                "max_positions_per_event": self.max_positions_per_event,
                "max_cost_per_event": self.max_cost_per_event,
                "max_cost_per_strike": self.max_cost_per_strike,
                "dry_run": self.dry_run,
            },
        )

    # --------------------
    # state
    # --------------------

    def _load_from_state_file(self) -> None:
        st = _read_state(self.state_file)
        if not isinstance(st, dict):
            return
        if st.get("schema") != "v2":
            return

        event_ticker = st.get("event_ticker")
        if isinstance(event_ticker, str):
            self.event_ticker = event_ticker

        ops = st.get("open_positions")
        if isinstance(ops, dict):
            self.open_positions = {str(k): v for k, v in ops.items() if isinstance(k, str) and isinstance(v, dict)}

        self.entered_markets = set(self.open_positions.keys())
        self.positions_count = len(self.open_positions)

        total_cost = 0.0
        for _, pos in self.open_positions.items():
            c = pos.get("entry_cost_dollars")
            if isinstance(c, (int, float)):
                total_cost += float(c)
        self.total_cost_event = float(total_cost)

    def _persist_state_file(self) -> None:
        out = {
            "schema": "v2",
            "event_ticker": self.event_ticker,
            "open_positions": self.open_positions,
            "total_cost_event": float(self.total_cost_event),
            "positions_count": int(self.positions_count),
            "ts_utc": _utc_ts(),
        }
        _write_state(self.state_file, out)

    def _add_position(self, cand: _Candidate, *, filled_count: int) -> None:
        entry_fee = float(filled_count) * (float(self.fee_cents) / 100.0)
        entry_cost = float(filled_count) * ((float(cand.price_cents) + float(self.fee_cents)) / 100.0)

        pos = {
            "market_ticker": cand.market_ticker,
            "event_ticker": cand.event_ticker,
            "side": cand.side,
            "count": int(filled_count),
            "entry_price_cents": int(cand.price_cents),
            "entry_cost_dollars": float(entry_cost),
            "entry_fee_dollars": float(entry_fee),
            "entry_ts_utc": _utc_ts(),
            "p_at_entry": float(cand.p_yes),
            "ev_at_entry": float(cand.ev),
            "edge_pp_at_entry": float(cand.edge_pp),
            "implied_q_yes": float(cand.implied_q_yes) if cand.implied_q_yes is not None else None,
            "strike": float(cand.strike),
            "subtitle": str(cand.subtitle),
            "dry_run": self.dry_run,
        }

        self.open_positions[cand.market_ticker] = pos
        self.entered_markets.add(cand.market_ticker)
        self.positions_count = len(self.open_positions)
        self.total_cost_event = float(self.total_cost_event + entry_cost)
        self._persist_state_file()

    # --------------------
    # optional reconcile
    # --------------------

    def reconcile_state(self, event_ticker: str) -> None:
        event_ticker = event_ticker.upper()
        cursor = None
        market_positions: List[dict] = []
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

        found = 0
        for mp in market_positions:
            tkr = mp.get("ticker") or mp.get("market_ticker")
            pos = mp.get("position")
            if not isinstance(tkr, str):
                continue
            try:
                if isinstance(pos, bool) or not isinstance(pos, (int, float, str)):
                    continue
                pos_val = int(pos)
            except Exception:
                continue
            abs_pos = abs(pos_val)
            if abs_pos <= 0:
                continue
            side = "yes" if pos_val > 0 else "no"

            if tkr in self.entered_markets:
                continue

            self.open_positions[tkr] = {
                "market_ticker": tkr,
                "event_ticker": event_ticker,
                "side": side,
                "count": int(abs_pos),
                "entry_price_cents": None,
                "entry_cost_dollars": None,
                "entry_fee_dollars": None,
                "entry_ts_utc": None,
                "p_at_entry": None,
                "ev_at_entry": None,
                "edge_pp_at_entry": None,
                "implied_q_yes": None,
                "strike": None,
                "subtitle": None,
                "reconciled": True,
                "reconciled_ts_utc": _utc_ts(),
                "dry_run": self.dry_run,
            }
            self.entered_markets.add(tkr)
            found += 1

        self.event_ticker = event_ticker
        self.positions_count = len(self.open_positions)

        if found > 0:
            self.total_cost_event = float(max(self.total_cost_event, self.max_cost_per_event))
            self._persist_state_file()

        self.log.log(
            "reconcile_done",
            {
                "event_ticker": event_ticker,
                "positions_seen": len(market_positions),
                "positions_added": int(found),
                "positions_count": int(self.positions_count),
                "total_cost_event": float(self.total_cost_event),
            },
        )
        print(f"[TRADE] reconcile: added={found} positions_count={self.positions_count}")

    # --------------------
    # candidate extraction + gating
    # --------------------

    def _row_best(
        self, row
    ) -> Tuple[Optional[str], Optional[int], Optional[float], Optional[int], Optional[float], Optional[float]]:
        ev_yes = row.ev_yes
        ev_no = row.ev_no

        best_side = None
        best_ev = None
        if ev_yes is not None:
            best_side, best_ev = "yes", float(ev_yes)
        if ev_no is not None and (best_ev is None or float(ev_no) > best_ev):
            best_side, best_ev = "no", float(ev_no)

        if best_side is None or best_ev is None:
            return None, None, None, None, None, None

        if best_side == "yes":
            price_cents = row.ob.ybuy
            spread_cents = row.ob.spread_y
            top_size = row.ob.nqty
        else:
            price_cents = row.ob.nbuy
            spread_cents = row.ob.spread_n
            top_size = row.ob.yqty

        if price_cents is None:
            return None, None, None, None, None, None

        implied_q_yes = (float(row.ob.ybuy) / 100.0) if row.ob.ybuy is not None else None

        return best_side, int(price_cents), float(best_ev), spread_cents, top_size, implied_q_yes

    def _build_candidates(self, result: EvaluationResult) -> List[_Candidate]:
        out: List[_Candidate] = []
        for row in result.rows:
            best_side, price_cents, best_ev, spread_cents, top_size, implied_q_yes = self._row_best(row)
            if best_side is None or price_cents is None or best_ev is None:
                continue

            if best_ev < self.ev_min:
                continue

            out.append(
                _Candidate(
                    market_ticker=str(row.ticker),
                    event_ticker=str(result.event_ticker),
                    side=str(best_side),
                    count=int(self.count),
                    price_cents=int(price_cents),
                    fee_cents=int(self.fee_cents),
                    p_yes=float(row.p_model),
                    strike=float(row.strike),
                    subtitle=str(row.subtitle),
                    implied_q_yes=implied_q_yes,
                    edge_pp=float(best_ev),
                    ev=float(best_ev),
                    spread_cents=int(spread_cents) if spread_cents is not None else None,
                    top_size=float(top_size) if top_size is not None else None,
                )
            )
        out.sort(key=lambda c: c.ev, reverse=True)
        return out

    def _skip(self, cand: _Candidate, reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "event_ticker": cand.event_ticker,
            "market_ticker": cand.market_ticker,
            "side": cand.side,
            "count": int(cand.count),
            "price_cents": int(cand.price_cents),
            "fee_cents": int(self.fee_cents),
            "p": float(cand.p_yes),
            "implied_q_yes": float(cand.implied_q_yes) if cand.implied_q_yes is not None else None,
            "edge_pp": float(cand.edge_pp),
            "EV": float(cand.ev),
            "spread_cents": int(cand.spread_cents) if cand.spread_cents is not None else None,
            "top_size": float(cand.top_size) if cand.top_size is not None else None,
            "skip_reason": str(reason),
            "dry_run": self.dry_run,
        }
        if extra:
            payload.update(extra)
        self.log.log("skip", payload)

    def _cap_check(self, cand: _Candidate) -> Optional[str]:
        if self.event_ticker is not None and self.event_ticker != cand.event_ticker:
            return "state_event_mismatch"
        if cand.market_ticker in self.entered_markets:
            return "already_entered_market"
        if self.positions_count >= self.max_positions_per_event:
            return "max_positions_reached"
        if cand.total_cost_dollars > self.max_cost_per_strike:
            return "max_cost_per_strike"
        if (self.total_cost_event + cand.total_cost_dollars) > self.max_cost_per_event:
            return "max_cost_per_event"
        return None

    def _liquidity_check(self, cand: _Candidate) -> Optional[str]:
        if self.spread_max_cents is not None and cand.spread_cents is not None and cand.spread_cents > self.spread_max_cents:
            return "spread_too_wide"
        if cand.top_size is not None and cand.top_size < self.min_top_size:
            return "top_size_too_small"
        return None

    # --------------------
    # order
    # --------------------

    def _submit_entry(self, cand: _Candidate) -> None:
        payload: Dict[str, Any] = {
            "ticker": cand.market_ticker,
            "action": "buy",
            "side": cand.side,
            "count": int(cand.count),
            "type": "limit",
            "time_in_force": "fill_or_kill",
            "client_order_id": str(uuid.uuid4()),
        }
        if cand.side == "yes":
            payload["yes_price"] = int(cand.price_cents)
        else:
            payload["no_price"] = int(cand.price_cents)

        self.log.log(
            "order_submit",
            {
                "event_ticker": cand.event_ticker,
                "market_ticker": cand.market_ticker,
                "side": cand.side,
                "count": int(cand.count),
                "price_cents": int(cand.price_cents),
                "entry_cost": float(cand.total_cost_dollars),
                "fee": float(cand.total_fee_dollars),
                "p": float(cand.p_yes),
                "implied_q": float(cand.implied_q) if cand.implied_q is not None else None,
                "edge_pp": float(cand.edge_pp),
                "EV": float(cand.ev),
                "dry_run": self.dry_run,
            },
        )

        if self.dry_run:
            self._add_position(cand, filled_count=int(cand.count))
            self.log.log(
                "entry_filled",
                {
                    "event_ticker": cand.event_ticker,
                    "market_ticker": cand.market_ticker,
                    "side": cand.side,
                    "count": int(cand.count),
                    "price_cents": int(cand.price_cents),
                    "entry_cost": float(cand.total_cost_dollars),
                    "fee": float(cand.total_fee_dollars),
                    "p": float(cand.p_yes),
                    "implied_q": float(cand.implied_q) if cand.implied_q is not None else None,
                    "edge_pp": float(cand.edge_pp),
                    "EV": float(cand.ev),
                    "dry_run": True,
                },
            )
            return

        try:
            resp = create_order(self.http, self.auth, payload, base_url=self.kalshi_base_url)
        except Exception as e:
            self.log.log(
                "entry_rejected",
                {
                    "event_ticker": cand.event_ticker,
                    "market_ticker": cand.market_ticker,
                    "side": cand.side,
                    "count": int(cand.count),
                    "price_cents": int(cand.price_cents),
                    "error": str(e),
                    "dry_run": False,
                },
            )
            return

        order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
        status = str(order.get("status")) if isinstance(order, dict) else None
        fill_count = order.get("fill_count") if isinstance(order, dict) else None

        if _is_filled(resp, int(cand.count)):
            self._add_position(cand, filled_count=int(cand.count))
            self.log.log(
                "entry_filled",
                {
                    "event_ticker": cand.event_ticker,
                    "market_ticker": cand.market_ticker,
                    "side": cand.side,
                    "count": int(cand.count),
                    "price_cents": int(cand.price_cents),
                    "entry_cost": float(cand.total_cost_dollars),
                    "fee": float(cand.total_fee_dollars),
                    "p": float(cand.p_yes),
                    "implied_q": float(cand.implied_q) if cand.implied_q is not None else None,
                    "edge_pp": float(cand.edge_pp),
                    "EV": float(cand.ev),
                    "status": status,
                    "fill_count": fill_count,
                    "dry_run": False,
                },
            )
        else:
            self.log.log(
                "entry_rejected",
                {
                    "event_ticker": cand.event_ticker,
                    "market_ticker": cand.market_ticker,
                    "side": cand.side,
                    "count": int(cand.count),
                    "price_cents": int(cand.price_cents),
                    "status": status,
                    "fill_count": fill_count,
                    "dry_run": False,
                },
            )

    # --------------------
    # main tick
    # --------------------

    def on_tick(self, result: EvaluationResult) -> None:
        if self.event_ticker is None:
            self.event_ticker = result.event_ticker
            self._persist_state_file()

        cands = self._build_candidates(result)
        submitted = 0
        stop_reason: Optional[str] = None

        for i, cand in enumerate(cands):
            cap_reason = self._cap_check(cand)
            if cap_reason is not None:
                self._skip(cand, cap_reason, {"total_cost_event": float(self.total_cost_event)})
                continue

            liq_reason = self._liquidity_check(cand)
            if liq_reason is not None:
                self._skip(cand, liq_reason)
                continue

            self._submit_entry(cand)
            submitted += 1

            if self.positions_count >= self.max_positions_per_event:
                stop_reason = "max_positions_reached"
            elif self.total_cost_event >= self.max_cost_per_event:
                stop_reason = "max_cost_per_event"

            if stop_reason is not None:
                for cand2 in cands[i + 1 :]:
                    self._skip(cand2, stop_reason, {"total_cost_event": float(self.total_cost_event)})
                break

        print(
            f"[TRADE] event={result.event_ticker} markets={len(result.rows)} candidates={len(cands)} "
            f"submitted={submitted} total_cost_event=${self.total_cost_event:.4f} positions={self.positions_count}"
        )

    def snapshot_state(self) -> Dict[str, Any]:
        return {
            "schema": "v2",
            "event_ticker": self.event_ticker,
            "positions_count": int(self.positions_count),
            "total_cost_event": float(self.total_cost_event),
            "open_positions": self.open_positions,
        }

    def on_shutdown(self, last_result: Optional[EvaluationResult] = None) -> None:
        snap = self.snapshot_state()
        if last_result is not None:
            snap.update({"spot": float(last_result.market_state.spot), "minutes_left": float(last_result.minutes_left)})
        self.log.log("bot_shutdown", snap)


# -------------------------------------------------------------------
# Canonical v2 (config-driven) exports.
#
# New Workflow B: all strategy parameters come from `StrategyConfig`, loaded from
# `KALSHI_EDGE_CONFIG_JSON` when set.
# -------------------------------------------------------------------

from kalshi_edge.strategy_config import StrategyConfig as StrategyConfig  # noqa: E402,F401
from kalshi_edge.strategy_config import load_config as load_config  # noqa: E402,F401
from kalshi_edge.trader_v2_engine import V2Trader as V2Trader  # noqa: E402,F401
from kalshi_edge.trader_v2_engine import debug_order_manager as debug_order_manager  # noqa: E402,F401

__all__ = ["V2Trader", "StrategyConfig", "load_config", "debug_order_manager"]

