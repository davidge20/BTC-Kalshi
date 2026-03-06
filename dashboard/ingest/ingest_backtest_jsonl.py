from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dashboard.config import ensure_parent_dir, load_dashboard_config
from dashboard.ingest.mapping import (
    as_float,
    as_int,
    json_dumps,
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


def _run_id_for_file(path: str) -> str:
    h = hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:12]
    return f"backtest-{h}"


def _iso_to_ts_utc(s: Any) -> Optional[str]:
    if not isinstance(s, str) or not s:
        return None
    # Backtest uses "Z" suffix; dashboard stores ISO strings; keep as-is.
    return s


def ingest_backtest_jsonl(*, input_path: str, db_path: str, run_id: Optional[str] = None) -> int:
    """
    Ingest kalshi_edge backtest JSONL logs (record_type=entry/event_summary/run_summary)
    into the dashboard SQLite.
    """
    rid = run_id or _run_id_for_file(input_path)
    conn = open_db(db_path)
    n = 0

    with conn:
        for rec in _iter_jsonl(input_path):
            rtype = rec.get("record_type")
            if not isinstance(rtype, str):
                continue

            if rtype == "ladder":
                ts_utc = _iso_to_ts_utc(rec.get("ts"))
                event_ticker = rec.get("event")
                rows = rec.get("rows")
                if ts_utc is None or not isinstance(event_ticker, str) or not isinstance(rows, list):
                    continue

                payload = {
                    **rec,
                    "ts_utc": ts_utc,
                    "event": "backtest_ladder",
                    "run_id": rid,
                    "event_ticker": event_ticker,
                }
                insert_event(conn, ts_utc=ts_utc, event="backtest_ladder", payload=payload)

                fee_cents = as_int(rec.get("fee_cents"))
                minutes_left = as_float(rec.get("minutes_left"))
                spot = as_float(rec.get("spot"))
                sigma = as_float(rec.get("sigma"))

                cand_rows = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    mt = r.get("market_ticker")
                    if not isinstance(mt, str):
                        continue
                    strike = as_float(r.get("strike")) or parse_strike_from_market_ticker(mt)
                    price_cents = as_int(r.get("price_cents"))
                    implied_q_yes = as_float(r.get("implied_q_yes"))
                    p_yes = as_float(r.get("p_yes"))
                    ev_yes = as_float(r.get("ev_yes"))
                    spread_cents = as_int(r.get("spread_cents"))

                    cand_rows.append(
                        (
                            ts_utc,
                            rid,
                            event_ticker,
                            mt,
                            "yes",
                            strike,
                            price_cents,
                            fee_cents,
                            p_yes,
                            implied_q_yes,
                            ev_yes,
                            ev_yes,
                            spread_cents,
                            None,
                            minutes_left,
                            spot,
                            sigma,
                            "backtest",
                            "backtest_ladder",
                        )
                    )

                if cand_rows:
                    conn.executemany(
                        """
                        INSERT INTO candidates(
                          ts_utc, run_id, event_ticker, market_ticker, side,
                          strike, price_cents, fee_cents, p_model, implied_q_yes,
                          edge_pp, ev, spread_cents, top_size, minutes_left, spot, sigma_blend, source, kind
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        cand_rows,
                    )

                # Also store tick context to system_health for freshness/diagnostics
                for k, v in (("tick.spot", spot), ("tick.sigma_blend", sigma), ("tick.minutes_left", minutes_left)):
                    if v is None:
                        continue
                    conn.execute(
                        """
                        INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (ts_utc, rid, k, float(v), None, json_dumps(payload)),
                    )

                n += 1
                continue

            if rtype == "entry":
                ts_utc = _iso_to_ts_utc(rec.get("ts"))
                if ts_utc is None:
                    continue
                event_ticker = rec.get("event")
                market_ticker = rec.get("market_ticker")
                side = rec.get("side")
                if not (isinstance(event_ticker, str) and isinstance(market_ticker, str) and isinstance(side, str)):
                    continue

                # Store raw event too (for traceability)
                payload = {**rec, "ts_utc": ts_utc, "event": "backtest_entry", "run_id": rid, "event_ticker": event_ticker}
                insert_event(conn, ts_utc=ts_utc, event="backtest_entry", payload=payload)

                contracts = as_int(rec.get("contracts")) or 0
                entry_price_cents = as_int(rec.get("entry_price_cents"))
                fee_cents = as_int(rec.get("fee_cents"))
                p_yes = as_float(rec.get("p_yes"))
                p_win = as_float(rec.get("p_win"))
                ev = as_float(rec.get("ev"))

                # candidates row (so Market Edge tab has something to show historically)
                implied_q_yes = None
                if entry_price_cents is not None:
                    q = float(entry_price_cents) / 100.0
                    implied_q_yes = q if side == "yes" else (1.0 - q)

                strike = parse_strike_from_market_ticker(market_ticker)
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
                        rid,
                        event_ticker,
                        market_ticker,
                        side,
                        strike,
                        entry_price_cents,
                        fee_cents,
                        p_yes,
                        implied_q_yes,
                        ev,
                        ev,
                        as_int(rec.get("spread")),
                        None,
                        None,
                        None,
                        None,
                        "backtest",
                        "backtest_entry",
                    ),
                )

                # fill row
                conn.execute(
                    """
                    INSERT INTO fills(
                      ts_utc, run_id, event_ticker, market_ticker, order_id, side, fill_kind,
                      count, price_cents, fee_cents, edge_pp, pnl_total, pnl_per_contract, payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ts_utc,
                        rid,
                        event_ticker,
                        market_ticker,
                        None,
                        side,
                        "entry",
                        int(contracts),
                        entry_price_cents,
                        fee_cents,
                        ev,
                        None,
                        None,
                        json_dumps(payload),
                    ),
                )

                n += 1
                continue

            if rtype == "event_summary":
                event_ticker = rec.get("event")
                if not isinstance(event_ticker, str):
                    continue
                close_ts = _iso_to_ts_utc(rec.get("close_ts"))
                ts_utc = close_ts or "1970-01-01T00:00:00Z"
                pnl = as_float(rec.get("pnl"))

                payload = {**rec, "ts_utc": ts_utc, "event": "backtest_event_summary", "run_id": rid, "event_ticker": event_ticker}
                insert_event(conn, ts_utc=ts_utc, event="backtest_event_summary", payload=payload)

                if pnl is not None:
                    conn.execute(
                        """
                        INSERT INTO pnl(ts_utc, run_id, event_ticker, market_ticker, realized, unrealized, total, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (ts_utc, rid, event_ticker, None, float(pnl), None, float(pnl), json_dumps(payload)),
                    )
                n += 1
                continue

            if rtype == "run_summary":
                ts_utc = _iso_to_ts_utc(rec.get("end_ts")) or _iso_to_ts_utc(rec.get("start_ts")) or "1970-01-01T00:00:00Z"
                payload = {**rec, "ts_utc": ts_utc, "event": "backtest_run_summary", "run_id": rid}
                insert_event(conn, ts_utc=ts_utc, event="backtest_run_summary", payload=payload)

                # system health / metadata
                for k in ("series_ticker", "start_ts", "end_ts", "events_scanned", "events_simulated", "trades", "contracts", "total_pnl", "win_rate"):
                    v = rec.get(k)
                    if v is None:
                        continue
                    value_num = as_float(v)
                    value_text = None if value_num is not None else str(v)
                    conn.execute(
                        """
                        INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (ts_utc, rid, f"backtest.{k}", value_num, value_text, json_dumps(payload)),
                    )

                cfg = rec.get("config")
                if cfg is not None:
                    conn.execute(
                        """
                        INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (ts_utc, rid, "backtest.config_json", None, json.dumps(cfg), json_dumps(payload)),
                    )

                n += 1
                continue

            if rtype == "progress":
                ts_utc = _iso_to_ts_utc(rec.get("ts")) or "1970-01-01T00:00:00Z"
                event_ticker = rec.get("event")
                payload = {
                    **rec,
                    "ts_utc": ts_utc,
                    "event": "backtest_progress",
                    "run_id": rid,
                    "event_ticker": event_ticker if isinstance(event_ticker, str) else None,
                }
                insert_event(conn, ts_utc=ts_utc, event="backtest_progress", payload=payload)

                for k in ("events_total", "events_scanned", "events_simulated"):
                    v = as_float(rec.get(k))
                    if v is None:
                        continue
                    conn.execute(
                        """
                        INSERT INTO system_health(ts_utc, run_id, metric, value_num, value_text, payload_json)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (ts_utc, rid, f"backtest.progress.{k}", float(v), None, json_dumps(payload)),
                    )

                n += 1
                continue

            # Unknown record_type: store raw for forward compat
            ts_utc = _iso_to_ts_utc(rec.get("ts")) or _iso_to_ts_utc(rec.get("end_ts")) or "1970-01-01T00:00:00Z"
            payload = {**rec, "ts_utc": ts_utc, "event": "backtest_unknown", "run_id": rid}
            insert_event(conn, ts_utc=ts_utc, event="backtest_unknown", payload=payload)
            n += 1

    conn.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="dashboard.ingest_backtest_jsonl",
        description="Ingest kalshi_edge backtest JSONL (record_type=...) into dashboard SQLite.",
    )
    ap.add_argument("--input", required=True, type=str, help="Path to backtest JSONL log (backtest_*.jsonl)")
    ap.add_argument("--db", required=False, type=str, default=None, help="SQLite DB path (default from dashboard config)")
    ap.add_argument("--config", required=False, type=str, default=None, help="Dashboard config JSON path")
    ap.add_argument("--run-id", required=False, type=str, default=None, help="Optional run_id label to store")
    args = ap.parse_args()

    cfg = load_dashboard_config(args.config)
    db_path = args.db or cfg.db_path
    ensure_parent_dir(db_path)

    n = ingest_backtest_jsonl(input_path=str(args.input), db_path=str(db_path), run_id=args.run_id)
    print(f"[dashboard] ingested {n} backtest records into {db_path}")


if __name__ == "__main__":
    main()

