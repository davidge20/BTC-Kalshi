from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dashboard.config import ensure_parent_dir, load_dashboard_config
from dashboard.ingest.mapping import (
    as_float,
    as_int,
    extract_edge_pp,
    extract_ev,
    extract_fee_cents,
    extract_implied_q_yes,
    extract_p_model_yes,
    extract_price_cents,
    fill_kind_for_event,
    infer_order_key,
    json_dumps,
    normalize_market_ticker,
    parse_strike_from_market_ticker,
)
from dashboard.storage import open_db
from dashboard.storage.db import insert_event


def _iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def ingest_jsonl(*, input_path: str, db_path: str) -> int:
    conn = open_db(db_path)
    n = 0

    with conn:
        for rec in _iter_jsonl(input_path):
            ts_utc = rec.get("ts_utc")
            event = rec.get("event")
            if not isinstance(ts_utc, str) or not isinstance(event, str):
                continue

            insert_event(conn, ts_utc=ts_utc, event=event, payload=rec)
            _route_derived(conn, ts_utc=ts_utc, event=event, payload=rec)
            n += 1

    conn.close()
    return n


def _route_derived(conn, *, ts_utc: str, event: str, payload: Dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    event_ticker = payload.get("event_ticker")
    market_ticker = normalize_market_ticker(payload)
    side = payload.get("side")

    # --- legacy logs: entry_signal (treat as a candidate row)
    if event == "entry_signal":
        if isinstance(event_ticker, str) and isinstance(market_ticker, str) and isinstance(side, str):
            price_cents = as_int(payload.get("buy_cents")) or extract_price_cents(payload)
            fee_cents = extract_fee_cents(payload)
            p_win_entry = as_float(payload.get("p_win_entry"))
            p_yes = None
            implied_q_yes = None
            if p_win_entry is not None:
                p_yes = p_win_entry if side == "yes" else (1.0 - p_win_entry)
            if price_cents is not None:
                q = float(price_cents) / 100.0
                implied_q_yes = q if side == "yes" else (1.0 - q)

            strike = as_float(payload.get("strike")) or parse_strike_from_market_ticker(market_ticker)
            conn.execute(
                """
                INSERT INTO candidates(
                  ts_utc, run_id, event_ticker, market_ticker, side,
                  strike, price_cents, fee_cents, p_model, implied_q_yes,
                  edge_pp, ev, spread_cents, top_size, minutes_left, spot, sigma_blend, source, kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts_utc,
                    run_id,
                    event_ticker,
                    market_ticker,
                    side,
                    strike,
                    price_cents,
                    fee_cents,
                    p_yes,
                    implied_q_yes,
                    extract_edge_pp(payload),
                    extract_ev(payload),
                    None,
                    None,
                    as_float(payload.get("minutes_left")),
                    as_float(payload.get("spot")),
                    None,
                    payload.get("source") if isinstance(payload.get("source"), str) else "legacy",
                    "entry_signal",
                ),
            )
        return

    # --- candidates / skips (ladder rows)
    if event in {"candidate", "skip"}:
        if isinstance(event_ticker, str) and isinstance(market_ticker, str) and isinstance(side, str):
            strike = as_float(payload.get("strike")) or parse_strike_from_market_ticker(market_ticker)
            conn.execute(
                """
                INSERT INTO candidates(
                  ts_utc, run_id, event_ticker, market_ticker, side,
                  strike, price_cents, fee_cents, p_model, implied_q_yes,
                  edge_pp, ev, spread_cents, top_size, minutes_left, spot, sigma_blend, source, kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts_utc,
                    run_id,
                    event_ticker,
                    market_ticker,
                    side,
                    strike,
                    extract_price_cents(payload),
                    extract_fee_cents(payload),
                    extract_p_model_yes(payload),
                    extract_implied_q_yes(payload),
                    extract_edge_pp(payload),
                    extract_ev(payload),
                    as_int(payload.get("spread_cents")),
                    as_float(payload.get("top_size")),
                    as_float(payload.get("minutes_left")),
                    as_float(payload.get("spot")),
                    as_float(payload.get("sigma_blend")),
                    payload.get("source") if isinstance(payload.get("source"), str) else None,
                    event,
                ),
            )
        return

    # --- orders and lifecycle
    if event.startswith("order_") or event in {"fill_detected"}:
        order_id = infer_order_key(payload)
        conn.execute(
            """
            INSERT INTO order_events(
              ts_utc, run_id, order_id, client_order_id, event_ticker, market_ticker, side,
              action, status, price_cents, count, remaining_count, delta_fill_count, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts_utc,
                run_id,
                order_id,
                payload.get("client_order_id") if isinstance(payload.get("client_order_id"), str) else None,
                event_ticker if isinstance(event_ticker, str) else None,
                market_ticker if isinstance(market_ticker, str) else None,
                side if isinstance(side, str) else None,
                payload.get("action") if isinstance(payload.get("action"), str) else None,
                payload.get("status") if isinstance(payload.get("status"), str) else None,
                extract_price_cents(payload),
                as_int(payload.get("count")) or as_int(payload.get("new_count")),
                as_int(payload.get("remaining_count")),
                as_int(payload.get("delta_fill_count")),
                json_dumps(payload),
            ),
        )

        # minimal system health capture for failures
        if event.endswith("_failed") and isinstance(payload.get("error"), str):
            conn.execute(
                """
                INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (ts_utc, run_id, "error", None, str(payload.get("error")), json_dumps(payload)),
            )
        return

    # --- fills
    fk = fill_kind_for_event(event)
    if fk is not None:
        # count field varies by event
        if event == "exit_filled":
            count = as_int(payload.get("sell_count")) or 0
            price_cents = as_int(payload.get("exit_bid_cents")) or extract_price_cents(payload)
        else:
            count = as_int(payload.get("count")) or 0
            price_cents = extract_price_cents(payload)

        conn.execute(
            """
            INSERT INTO fills(
              ts_utc, run_id, event_ticker, market_ticker, order_id, side, fill_kind,
              count, price_cents, fee_cents, edge_pp, pnl_total, pnl_per_contract, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts_utc,
                run_id,
                event_ticker if isinstance(event_ticker, str) else None,
                market_ticker if isinstance(market_ticker, str) else None,
                infer_order_key(payload),
                side if isinstance(side, str) else None,
                fk,
                int(count),
                price_cents,
                extract_fee_cents(payload),
                extract_edge_pp(payload),
                as_float(payload.get("pnl_total")),
                as_float(payload.get("pnl_per_contract")),
                json_dumps(payload),
            ),
        )

        # also store pnl point (trade-level) if present
        pnl_total = as_float(payload.get("pnl_total"))
        if pnl_total is not None:
            conn.execute(
                """
                INSERT INTO pnl(ts_utc, run_id, event_ticker, market_ticker, realized, unrealized, total, payload_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_utc,
                    run_id,
                    event_ticker if isinstance(event_ticker, str) else None,
                    market_ticker if isinstance(market_ticker, str) else None,
                    pnl_total,
                    None,
                    pnl_total,
                    json_dumps(payload),
                ),
            )
        return

    # --- system / tick summary
    if event in {"bot_start", "tick_summary", "bot_shutdown", "event_settled"}:
        # Config metadata
        if event == "bot_start":
            for k in ("config_hash", "git_commit", "strategy_name", "strategy_schema_version"):
                v = payload.get(k)
                if v is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (ts_utc, run_id, f"bot_start.{k}", None, str(v), json_dumps(payload)),
                )

        if event == "tick_summary":
            for k in ("spot", "sigma_implied", "sigma_realized", "sigma_blend", "minutes_left", "confidence"):
                v = as_float(payload.get(k))
                if v is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (ts_utc, run_id, f"tick.{k}", float(v), None, json_dumps(payload)),
                )

        if event == "event_settled":
            total = as_float(payload.get("pnl_total"))
            if total is not None:
                conn.execute(
                    """
                    INSERT INTO pnl(ts_utc, run_id, event_ticker, market_ticker, realized, unrealized, total, payload_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts_utc,
                        run_id,
                        event_ticker if isinstance(event_ticker, str) else None,
                        None,
                        total,
                        None,
                        total,
                        json_dumps(payload),
                    ),
                )


def main() -> None:
    ap = argparse.ArgumentParser(prog="dashboard.ingest_jsonl", description="Ingest kalshi_edge JSONL logs into dashboard SQLite.")
    ap.add_argument("--input", required=True, type=str, help="Path to JSONL file (trade log)")
    ap.add_argument("--db", required=False, type=str, default=None, help="SQLite DB path (default from dashboard config)")
    ap.add_argument("--config", required=False, type=str, default=None, help="Dashboard config JSON path")
    args = ap.parse_args()

    cfg = load_dashboard_config(args.config)
    db_path = args.db or cfg.db_path
    ensure_parent_dir(db_path)

    n = ingest_jsonl(input_path=str(args.input), db_path=str(db_path))
    print(f"[dashboard] ingested {n} events into {db_path}")


if __name__ == "__main__":
    main()

