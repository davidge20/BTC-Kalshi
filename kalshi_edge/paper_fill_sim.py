"""
paper_fill_sim.py

Dry-run (paper) fill simulator for resting (maker) orders.

Design goals:
- deterministic when seeded
- minimal / configurable model
- emit FillDelta through the same downstream path as real fills

Notes on order books:
- v2 engine has side-specific books (YES and NO). The simulator is book-agnostic:
  the caller should pass the relevant best bid/ask for the order's side via `update_book()`
  immediately before calling `maybe_fill()`.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from kalshi_edge.fill_delta import FillDelta
from kalshi_edge.strategy_config import PaperConfig


def _parse_ts(ts_utc: str) -> Optional[datetime]:
    if not isinstance(ts_utc, str) or not ts_utc:
        return None
    try:
        return datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    except Exception:
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperFillSimulator:
    def __init__(self, cfg: PaperConfig, rng: random.Random, *, fee_cents_per_contract: int):
        self.cfg = cfg
        self.rng = rng
        self.fee_cents_per_contract = int(fee_cents_per_contract)

        # (market_ticker -> (best_bid_cents, best_ask_cents, ts_utc))
        self._book: Dict[str, Tuple[Optional[int], Optional[int], str]] = {}

        # order_id -> timestamps/state
        self._eligible_since_ts: Dict[str, str] = {}
        self._last_tick_ts: Dict[str, str] = {}

        # order_id -> metadata about the last synthetic fill (for logging)
        self._last_fill_meta: Dict[str, Dict[str, Any]] = {}

    def update_book(self, market_ticker: str, best_bid_cents: Optional[int], best_ask_cents: Optional[int], ts_utc: str) -> None:
        self._book[str(market_ticker)] = (
            int(best_bid_cents) if best_bid_cents is not None else None,
            int(best_ask_cents) if best_ask_cents is not None else None,
            str(ts_utc) if isinstance(ts_utc, str) and ts_utc else _utc_now_iso(),
        )

    def pop_last_fill_meta(self, order_id: str) -> Optional[Dict[str, Any]]:
        return self._last_fill_meta.pop(str(order_id), None)

    def _cleanup_order(self, order_id: str) -> None:
        oid = str(order_id)
        self._eligible_since_ts.pop(oid, None)
        self._last_tick_ts.pop(oid, None)
        self._last_fill_meta.pop(oid, None)

    def _tick_ok(self, order_id: str, ts_utc: str) -> bool:
        tick_s = float(self.cfg.tick_seconds)
        if tick_s <= 0:
            return True
        prev = self._last_tick_ts.get(str(order_id))
        if not prev:
            self._last_tick_ts[str(order_id)] = str(ts_utc)
            return True
        dt_prev = _parse_ts(prev)
        dt_now = _parse_ts(ts_utc)
        if dt_prev is None or dt_now is None:
            self._last_tick_ts[str(order_id)] = str(ts_utc)
            return True
        if (dt_now - dt_prev).total_seconds() >= tick_s:
            self._last_tick_ts[str(order_id)] = str(ts_utc)
            return True
        return False

    def maybe_fill(self, tracked_order: Dict[str, Any], ts_utc: str) -> Optional[FillDelta]:
        """
        Potentially produce a synthetic fill for a resting maker order.
        Mutates tracked_order's fill/remaining/cost/fee/status fields when a fill occurs.
        """
        if not bool(self.cfg.simulate_maker_fills):
            return None

        oid = str(tracked_order.get("order_id") or "")
        if not oid:
            return None

        status = str(tracked_order.get("status", "")).lower()
        source = str(tracked_order.get("source", "")).lower()
        if source != "maker" or status != "resting":
            if status in {"canceled", "cancelled", "executed", "filled", "rejected", "expired", "error"}:
                self._cleanup_order(oid)
            return None

        remaining = int(tracked_order.get("remaining_count") or tracked_order.get("count") or 0)
        if remaining <= 0:
            self._cleanup_order(oid)
            return None

        if not self._tick_ok(oid, ts_utc):
            return None

        mkt = str(tracked_order.get("market_ticker") or "")
        if not mkt:
            return None
        best = self._book.get(mkt)
        if not best:
            return None
        best_bid_cents, best_ask_cents, _book_ts = best

        price_cents = int(tracked_order.get("price_cents") or 0)
        action = str(tracked_order.get("action") or "buy").lower()

        # Eligibility: at/through top-of-book. Caller is expected to provide the correct
        # book for the instrument side (YES vs NO); we only need bid/ask proxies here.
        at_top = (best_bid_cents is not None) and (price_cents >= int(best_bid_cents))
        crosses = (best_ask_cents is not None) and (price_cents >= int(best_ask_cents))

        if not at_top and not crosses:
            self._eligible_since_ts.pop(oid, None)
            return None

        # Crossing: treat as immediate execution (no waiting / randomness).
        if crosses:
            eligible_seconds = float(self.cfg.min_top_time_seconds)
            self._eligible_since_ts[oid] = ts_utc
        else:
            if oid not in self._eligible_since_ts:
                self._eligible_since_ts[oid] = ts_utc
            eligible_dt = _parse_ts(self._eligible_since_ts.get(oid, ""))
            now_dt = _parse_ts(ts_utc)
            if eligible_dt is None or now_dt is None:
                eligible_seconds = float(self.cfg.min_top_time_seconds)
            else:
                eligible_seconds = (now_dt - eligible_dt).total_seconds()

        if not crosses and eligible_seconds < float(self.cfg.min_top_time_seconds):
            return None

        rng_u = float(self.rng.random())
        if not crosses and rng_u >= float(self.cfg.fill_prob_per_tick):
            return None

        # Decide fill size.
        if bool(self.cfg.partial_fill):
            max_per = max(1, int(self.cfg.max_fill_per_tick))
            fill_size = min(int(remaining), int(self.rng.randint(1, max_per)))
        else:
            fill_size = int(remaining)
        fill_size = max(0, min(int(remaining), int(fill_size)))
        if fill_size <= 0:
            return None

        slip = int(self.cfg.slippage_cents)
        if action == "sell":
            fill_price_cents = int(price_cents) - int(slip)
        else:
            fill_price_cents = int(price_cents) + int(slip)
        fill_price_cents = max(0, min(99, int(fill_price_cents)))

        delta_cost = int(fill_size) * int(fill_price_cents)
        delta_fee = int(fill_size) * int(self.fee_cents_per_contract)
        delta = FillDelta(delta_fill_count=int(fill_size), delta_cost_cents=int(delta_cost), delta_fee_cents=int(delta_fee), ts_utc=str(ts_utc))

        tracked_order["fill_count"] = int(int(tracked_order.get("fill_count") or 0) + int(fill_size))
        tracked_order["remaining_count"] = int(max(0, int(remaining) - int(fill_size)))
        tracked_order["last_fill_cost_cents"] = int(int(tracked_order.get("last_fill_cost_cents") or 0) + int(delta_cost))
        tracked_order["last_fee_paid_cents"] = int(int(tracked_order.get("last_fee_paid_cents") or 0) + int(delta_fee))
        if int(tracked_order["remaining_count"]) <= 0:
            tracked_order["status"] = "executed"
            self._cleanup_order(oid)

        self._last_fill_meta[oid] = {
            "fill_price_cents": int(fill_price_cents),
            "fill_count": int(fill_size),
            "rng_u": float(rng_u),
            "reason": "crossed_top" if crosses else "eligible>=min_top_time && rng<p",
            "eligible_seconds": float(eligible_seconds),
        }

        return delta

