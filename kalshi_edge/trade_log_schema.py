"""
trade_log_schema.py

Lightweight (best-effort) schema contracts for the JSONL trade log.

This is intentionally minimal: it is *not* a full JSON Schema implementation.
The goal is to prevent silent field drift for the core events we analyze.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


LOG_SCHEMA_VERSION = "logschema_v1"


@dataclass(frozen=True)
class EventSchema:
    required_keys: List[str]
    optional_keys: List[str]


EVENT_SCHEMAS: Dict[str, EventSchema] = {
    # --- lifecycle ---
    "bot_start": EventSchema(
        required_keys=[
            "schema",
            "state_file",
            "trade_log_file",
        ],
        optional_keys=[
            "run_id",
            "strategy_name",
            "strategy_schema_version",
            "config_hash",
            "config",
            "config_path",
            "git_commit",
            "dry_run",
            "paper",
            "live",
            "subaccount",
            "full_config_on_start",
        ],
    ),
    "bot_shutdown": EventSchema(
        required_keys=[
            "schema",
        ],
        optional_keys=[
            "run_id",
            "strategy_name",
            "strategy_schema_version",
            "event_ticker",
            "minutes_left",
            "spot",
            "notes",
            "positions_count_all",
            "total_cost_all",
            "open_positions",
            "open_orders",
            "active_order_by_market",
        ],
    ),
    # --- execution ---
    "order_submit": EventSchema(
        required_keys=["market_ticker", "side", "count", "price_cents"],
        optional_keys=[
            "event_ticker",
            "source",
            "action",
            "mode",
            "tif",
            "time_in_force",
            "post_only",
            "reduce_only",
            "order_id",
            "client_order_id",
            "reason",
        ],
    ),
    "order_amend_submit": EventSchema(
        required_keys=["order_id", "new_price_cents", "new_count"],
        optional_keys=["reason", "market_ticker", "side", "source"],
    ),
    "order_cancel_submit": EventSchema(
        required_keys=["order_id", "reason"],
        optional_keys=["market_ticker", "side", "source"],
    ),
    "order_canceled": EventSchema(
        required_keys=["order_id"],
        optional_keys=["resp", "dry_run", "reason"],
    ),
    "order_amended": EventSchema(
        required_keys=["order_id"],
        optional_keys=["resp", "dry_run"],
    ),
    "order_submit_failed": EventSchema(
        required_keys=["error"],
        optional_keys=["market_ticker", "side", "source", "price_cents", "count"],
    ),
    "order_amend_failed": EventSchema(
        required_keys=["order_id", "error"],
        optional_keys=["new_price_cents", "new_count"],
    ),
    "order_cancel_failed": EventSchema(
        required_keys=["order_id", "error"],
        optional_keys=["reason"],
    ),
    # --- fills / trading semantics ---
    "fill_detected": EventSchema(
        required_keys=["order_id", "delta_fill_count"],
        optional_keys=["was_open", "market_ticker", "event_ticker", "side", "source"],
    ),
    "entry_filled": EventSchema(
        required_keys=["count"],
        optional_keys=["order_id", "market_ticker", "event_ticker", "side", "price_cents", "edge_pp", "p_yes"],
    ),
    "scale_in_filled": EventSchema(
        required_keys=["count"],
        optional_keys=["order_id", "market_ticker", "event_ticker", "side", "price_cents", "edge_pp", "p_yes"],
    ),
    "exit_signal": EventSchema(
        required_keys=["reason", "market_ticker", "side"],
        optional_keys=["minutes_left", "bid_cents", "net_exit_now", "sell_count"],
    ),
    "exit_filled": EventSchema(
        required_keys=["market_ticker", "side", "sell_count"],
        optional_keys=["reason", "exit_bid_cents", "exit_net", "entry_cost", "pnl_total", "pnl_per_contract"],
    ),
    # --- decision context ---
    "tick_summary": EventSchema(
        required_keys=["event_ticker", "minutes_left"],
        optional_keys=[
            "spot",
            "sigma_implied",
            "sigma_realized",
            "sigma_blend",
            "confidence",
            "num_rows_scanned",
            "top_ev",
            "top_edge_pp",
            "top_market_ticker",
            "top_side",
            "top_source",
        ],
    ),
    "candidate": EventSchema(
        required_keys=["event_ticker", "market_ticker", "side"],
        optional_keys=[
            "source",
            "price_cents",
            "fee_cents",
            "p_yes",
            "p_win",
            "implied_q_yes",
            "edge_pp",
            "ev",
            "bid_cents",
            "ask_proxy_cents",
            "spread_cents",
            "top_size",
            "strike",
            "subtitle",
            "minutes_left",
            "spot",
            "sigma_blend",
        ],
    ),
    "decision": EventSchema(
        required_keys=["action", "market_ticker", "side"],
        optional_keys=[
            "source",
            "price_cents",
            "count",
            "order_id",
            "reason",
            # candidate snapshot
            "p_yes",
            "p_win",
            "implied_q_yes",
            "edge_pp",
            "ev",
            "bid_cents",
            "ask_proxy_cents",
            "spread_cents",
            "top_size",
            "strike",
            "subtitle",
            "minutes_left",
            "spot",
            "sigma_blend",
        ],
    ),
    "skip": EventSchema(
        required_keys=["market_ticker", "side", "skip_reason"],
        optional_keys=[
            "source",
            "price_cents",
            "fee_cents",
            "p_yes",
            "p_win",
            "edge_pp",
            "ev",
            "spread_cents",
            "top_size",
            "order_id",
        ],
    ),
    # --- settlement ---
    "event_settled": EventSchema(
        required_keys=["event_ticker", "settled_ts_utc"],
        optional_keys=[
            "event_status",
            "outcome",
            "outcome_raw",
            "positions",
            "pnl_total",
            "pnl_by_market",
            "pnl_notes",
        ],
    ),
}

