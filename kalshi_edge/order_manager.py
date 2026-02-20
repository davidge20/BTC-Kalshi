"""
order_manager.py

Lightweight helper for maintaining *at most one* active order per (market_ticker, side).
It handles:
- create / amend / cancel
- periodic refresh via get_order()
- incremental fill deltas (count + cost + fee deltas)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from kalshi_edge.fill_delta import FillDelta
from kalshi_edge.http_client import HttpClient
from kalshi_edge.kalshi_api import amend_order, cancel_order, create_order, get_order
from kalshi_edge.trade_log import TradeLogger


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_side_key(market_ticker: str, side: str) -> str:
    return f"{market_ticker}|{side}"


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        if isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


class OrderManager:
    def __init__(
        self,
        *,
        http: HttpClient,
        auth,
        kalshi_base_url: str,
        log: TradeLogger,
        dry_run: bool,
        subaccount: Optional[int] = None,
    ):
        self.http = http
        self.auth = auth
        self.kalshi_base_url = kalshi_base_url
        self.log = log
        self.dry_run = bool(dry_run)
        self.subaccount = subaccount

    # --------------------
    # API wrappers
    # --------------------

    def api_get_order(self, order_id: str) -> Dict[str, Any]:
        return get_order(
            self.http,
            self.auth,
            order_id,
            base_url=self.kalshi_base_url,
            subaccount=self.subaccount,
        )

    def api_cancel_order(self, order_id: str) -> Dict[str, Any]:
        return cancel_order(
            self.http,
            self.auth,
            order_id,
            base_url=self.kalshi_base_url,
            subaccount=self.subaccount,
        )

    def api_amend_order(self, order_id: str, amend_data: Dict[str, Any]) -> Dict[str, Any]:
        return amend_order(
            self.http,
            self.auth,
            order_id,
            amend_data,
            base_url=self.kalshi_base_url,
            subaccount=self.subaccount,
        )

    def api_create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        return create_order(
            self.http,
            self.auth,
            order_data,
            base_url=self.kalshi_base_url,
            subaccount=self.subaccount,
        )

    # --------------------
    # tracked order helpers
    # --------------------

    def new_tracked_order(
        self,
        *,
        order_id: str,
        market_ticker: str,
        event_ticker: str,
        side: str,
        price_cents: int,
        count: int,
        time_in_force: str,
        post_only: bool,
        last_model_p: float,
        last_edge_pp: float,
        source: str,
        status: str,
        client_order_id: str,
    ) -> Dict[str, Any]:
        now = utc_ts()
        return {
            "order_id": str(order_id),
            "client_order_id": str(client_order_id),
            "market_ticker": str(market_ticker),
            "event_ticker": str(event_ticker),
            "side": str(side),
            "action": "buy",
            "type": "limit",
            "price_cents": int(price_cents),
            "count": int(count),
            "time_in_force": str(time_in_force),
            "post_only": bool(post_only),
            "source": str(source),  # "maker" or "taker"
            "status": str(status),
            "fill_count": 0,
            "remaining_count": int(count),
            "created_ts_utc": now,
            "last_checked_ts_utc": None,
            "last_amended_ts_utc": None,
            "last_model_p": float(last_model_p),
            "last_edge_pp": float(last_edge_pp),
            # For incremental fill deltas
            "last_fill_cost_cents": 0,
            "last_fee_paid_cents": 0,
        }

    def _order_obj(self, resp: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(resp.get("order"), dict):
            return resp["order"]
        return resp

    def refresh_tracked_order(self, tracked: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[FillDelta]]:
        """
        Refresh via GET and return (updated_tracked, optional_fill_delta).
        Fill delta uses Kalshi's aggregate fill cost + fees to compute increments.
        """
        if self.dry_run:
            tracked["last_checked_ts_utc"] = utc_ts()
            return tracked, None

        resp = self.api_get_order(str(tracked["order_id"]))
        o = self._order_obj(resp)

        status = str(o.get("status", tracked.get("status", ""))).lower()
        fill_count = _as_int(o.get("fill_count"), _as_int(tracked.get("fill_count"), 0))
        remaining_count = _as_int(o.get("remaining_count"), _as_int(tracked.get("remaining_count"), 0))

        total_cost = _as_int(o.get("maker_fill_cost"), 0) + _as_int(o.get("taker_fill_cost"), 0)
        total_fee = _as_int(o.get("maker_fees"), 0) + _as_int(o.get("taker_fees"), 0)

        prev_fill_count = _as_int(tracked.get("fill_count"), 0)
        prev_cost = _as_int(tracked.get("last_fill_cost_cents"), 0)
        prev_fee = _as_int(tracked.get("last_fee_paid_cents"), 0)

        tracked["status"] = status
        tracked["fill_count"] = int(fill_count)
        tracked["remaining_count"] = int(remaining_count)
        tracked["last_checked_ts_utc"] = utc_ts()

        delta: Optional[FillDelta] = None
        if fill_count > prev_fill_count:
            delta = FillDelta(
                delta_fill_count=int(fill_count - prev_fill_count),
                delta_cost_cents=int(total_cost - prev_cost),
                delta_fee_cents=int(total_fee - prev_fee),
                ts_utc=utc_ts(),
            )
            tracked["last_fill_cost_cents"] = int(total_cost)
            tracked["last_fee_paid_cents"] = int(total_fee)

        return tracked, delta

    def submit_new_order(
        self,
        *,
        market_ticker: str,
        event_ticker: str,
        side: str,
        price_cents: int,
        count: int,
        time_in_force: str,
        post_only: bool,
        source: str,
        last_model_p: float,
        last_edge_pp: float,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Create an order and return (tracked_order_dict, raw_response_dict).
        """
        client_order_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "ticker": str(market_ticker),
            "action": "buy",
            "side": str(side),
            "count": int(count),
            "type": "limit",
            "time_in_force": str(time_in_force),
            "client_order_id": client_order_id,
        }
        if post_only:
            payload["post_only"] = True

        if side == "yes":
            payload["yes_price"] = int(price_cents)
        else:
            payload["no_price"] = int(price_cents)

        if extra_payload:
            payload.update(extra_payload)

        if self.dry_run and source == "taker":
            tracked = self.new_tracked_order(
                order_id="DRYRUN-" + client_order_id,
                client_order_id=client_order_id,
                market_ticker=market_ticker,
                event_ticker=event_ticker,
                side=side,
                price_cents=price_cents,
                count=count,
                time_in_force=time_in_force,
                post_only=post_only,
                last_model_p=last_model_p,
                last_edge_pp=last_edge_pp,
                source=source,
                status="executed",
            )
            tracked["fill_count"] = int(count)
            tracked["remaining_count"] = 0
            return tracked, {"order": tracked}

        if self.dry_run and source == "maker":
            tracked = self.new_tracked_order(
                order_id="DRYRUN-" + client_order_id,
                client_order_id=client_order_id,
                market_ticker=market_ticker,
                event_ticker=event_ticker,
                side=side,
                price_cents=price_cents,
                count=count,
                time_in_force=time_in_force,
                post_only=post_only,
                last_model_p=last_model_p,
                last_edge_pp=last_edge_pp,
                source=source,
                status="resting",
            )
            return tracked, {"order": tracked}

        resp = self.api_create_order(payload)
        o = self._order_obj(resp)
        order_id = str(o.get("order_id") or o.get("id") or "")
        status = str(o.get("status", "")).lower()
        tracked = self.new_tracked_order(
            order_id=order_id or client_order_id,
            client_order_id=client_order_id,
            market_ticker=market_ticker,
            event_ticker=event_ticker,
            side=side,
            price_cents=price_cents,
            count=count,
            time_in_force=time_in_force,
            post_only=post_only,
            last_model_p=last_model_p,
            last_edge_pp=last_edge_pp,
            source=source,
            status=status or "unknown",
        )
        tracked["fill_count"] = _as_int(o.get("fill_count"), 0)
        tracked["remaining_count"] = _as_int(o.get("remaining_count"), int(count))
        tracked["last_fill_cost_cents"] = _as_int(o.get("maker_fill_cost"), 0) + _as_int(o.get("taker_fill_cost"), 0)
        tracked["last_fee_paid_cents"] = _as_int(o.get("maker_fees"), 0) + _as_int(o.get("taker_fees"), 0)
        tracked["last_checked_ts_utc"] = utc_ts()
        return tracked, resp

    def submit_amend(
        self,
        tracked: Dict[str, Any],
        *,
        new_price_cents: Optional[int] = None,
        new_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Submit an amend. Caller is responsible for gating (e.g. only resting orders).
        """
        if self.dry_run:
            if new_price_cents is not None:
                tracked["price_cents"] = int(new_price_cents)
            if new_count is not None:
                tracked["count"] = int(new_count)
                tracked["remaining_count"] = int(new_count)
            tracked["last_amended_ts_utc"] = utc_ts()
            tracked["status"] = str(tracked.get("status") or "resting")
            return {"order": tracked}

        updated_client_order_id = str(uuid.uuid4())
        amend_payload: Dict[str, Any] = {
            "ticker": str(tracked["market_ticker"]),
            "side": str(tracked["side"]),
            "action": "buy",
            "client_order_id": str(tracked["client_order_id"]),
            "updated_client_order_id": updated_client_order_id,
        }
        if new_price_cents is not None:
            if str(tracked["side"]) == "yes":
                amend_payload["yes_price"] = int(new_price_cents)
            else:
                amend_payload["no_price"] = int(new_price_cents)
        if new_count is not None:
            amend_payload["count"] = int(new_count)

        resp = self.api_amend_order(str(tracked["order_id"]), amend_payload)
        tracked["client_order_id"] = updated_client_order_id
        tracked["last_amended_ts_utc"] = utc_ts()
        if new_price_cents is not None:
            tracked["price_cents"] = int(new_price_cents)
        if new_count is not None:
            tracked["count"] = int(new_count)
            tracked["remaining_count"] = int(new_count)
        return resp

    def submit_cancel(self, tracked: Dict[str, Any]) -> Dict[str, Any]:
        if self.dry_run:
            tracked["status"] = "canceled"
            tracked["remaining_count"] = 0
            tracked["last_checked_ts_utc"] = utc_ts()
            return {"order": tracked}
        return self.api_cancel_order(str(tracked["order_id"]))

