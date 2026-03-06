from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _fetch_all(conn: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_event_tickers(conn: sqlite3.Connection, limit: int = 200) -> List[str]:
    rows = _fetch_all(
        conn,
        """
        SELECT event_ticker, MAX(ts_utc) AS last_ts
        FROM events
        WHERE event_ticker IS NOT NULL AND event_ticker <> ''
        GROUP BY event_ticker
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [str(r["event_ticker"]) for r in rows if r.get("event_ticker")]


def list_run_ids(conn: sqlite3.Connection, limit: int = 200) -> List[str]:
    """
    Distinct run IDs present in the DB, newest first.

    Backtests ingested via `ingest_backtest_jsonl` synthesize a stable run_id
    like "backtest-<hash>" per JSONL file.
    """
    rows = _fetch_all(
        conn,
        """
        SELECT run_id, MAX(ts_utc) AS last_ts
        FROM events
        WHERE run_id IS NOT NULL AND run_id <> ''
        GROUP BY run_id
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [str(r["run_id"]) for r in rows if r.get("run_id")]


def run_is_backtest(conn: sqlite3.Connection, *, run_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM events
        WHERE run_id = ?
          AND event LIKE 'backtest_%'
        LIMIT 1
        """,
        (str(run_id),),
    ).fetchone()
    return bool(row)


def list_event_tickers_for_run(conn: sqlite3.Connection, *, run_id: str, limit: int = 200) -> List[str]:
    rows = _fetch_all(
        conn,
        """
        SELECT event_ticker, MAX(ts_utc) AS last_ts
        FROM events
        WHERE run_id = ?
          AND event_ticker IS NOT NULL AND event_ticker <> ''
        GROUP BY event_ticker
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (str(run_id), int(limit)),
    )
    return [str(r["event_ticker"]) for r in rows if r.get("event_ticker")]


def list_markets_for_event(conn: sqlite3.Connection, event_ticker: str, limit: int = 500) -> List[str]:
    rows = _fetch_all(
        conn,
        """
        SELECT market_ticker, MAX(ts_utc) AS last_ts
        FROM events
        WHERE event_ticker = ? AND market_ticker IS NOT NULL AND market_ticker <> ''
        GROUP BY market_ticker
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (str(event_ticker), int(limit)),
    )
    return [str(r["market_ticker"]) for r in rows if r.get("market_ticker")]


def candidate_markets_for_event(
    conn: sqlite3.Connection,
    *,
    event_ticker: str,
    run_id: Optional[str] = None,
    side: str = "yes",
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    List distinct markets (optionally filtered by side) present in `candidates` for an event.
    Useful for strike-level drilldowns where `events` may not contain ladder rows.
    """
    where = ["event_ticker = ?", "market_ticker IS NOT NULL AND market_ticker <> ''", "side = ?"]
    params: List[Any] = [str(event_ticker), str(side)]
    if run_id:
        where.append("run_id = ?")
        params.append(str(run_id))
    sql = f"""
        SELECT
          market_ticker,
          strike,
          MAX(ts_utc) AS last_ts,
          COUNT(*) AS n_rows,
          COUNT(DISTINCT COALESCE(run_id, '')) AS n_runs
        FROM candidates
        WHERE {' AND '.join(where)}
        GROUP BY market_ticker, strike
        ORDER BY
          CASE WHEN strike IS NULL THEN 1 ELSE 0 END ASC,
          strike ASC,
          last_ts DESC
        LIMIT ?
    """
    params.append(int(limit))
    return _fetch_all(conn, sql, tuple(params))


def candidate_run_ids_for_market(conn: sqlite3.Connection, *, market_ticker: str, limit: int = 100) -> List[str]:
    rows = _fetch_all(
        conn,
        """
        SELECT run_id, MAX(ts_utc) AS last_ts
        FROM candidates
        WHERE market_ticker = ?
          AND run_id IS NOT NULL AND run_id <> ''
        GROUP BY run_id
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (str(market_ticker), int(limit)),
    )
    return [str(r["run_id"]) for r in rows if r.get("run_id")]


def candidate_series_for_market(
    conn: sqlite3.Connection,
    *,
    market_ticker: str,
    side: Optional[str] = "yes",
    run_id: Optional[str] = None,
    limit: int = 10_000,
) -> List[Dict[str, Any]]:
    """
    Time series of ladder/candidate rows for a specific market.
    """
    if run_id is not None and run_id != "":
        if side:
            return _fetch_all(
                conn,
                """
                SELECT *
                FROM candidates
                WHERE market_ticker = ?
                  AND side = ?
                  AND run_id = ?
                ORDER BY ts_utc ASC
                LIMIT ?
                """,
                (str(market_ticker), str(side), str(run_id), int(limit)),
            )
        return _fetch_all(
            conn,
            """
            SELECT *
            FROM candidates
            WHERE market_ticker = ?
              AND run_id = ?
            ORDER BY ts_utc ASC
            LIMIT ?
            """,
            (str(market_ticker), str(run_id), int(limit)),
        )

    if side:
        return _fetch_all(
            conn,
            """
            SELECT *
            FROM candidates
            WHERE market_ticker = ?
              AND side = ?
            ORDER BY ts_utc ASC
            LIMIT ?
            """,
            (str(market_ticker), str(side), int(limit)),
        )
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM candidates
        WHERE market_ticker = ?
        ORDER BY ts_utc ASC
        LIMIT ?
        """,
        (str(market_ticker), int(limit)),
    )


def latest_candidates(
    conn: sqlite3.Connection,
    *,
    event_ticker: str,
    run_id: Optional[str] = None,
    minutes: int = 120,
    min_edge_pp: float = 0.0,
) -> List[Dict[str, Any]]:
    # Note: Anchored to wall-clock time; best for live/paper runs.
    where = ["event_ticker = ?", "ts_utc >= datetime('now', ?)", "(edge_pp IS NULL OR edge_pp >= ?)"]
    params: List[Any] = [str(event_ticker), f"-{int(minutes)} minutes", float(min_edge_pp)]
    if run_id:
        where.append("run_id = ?")
        params.append(str(run_id))
    sql = f"""
        SELECT *
        FROM candidates
        WHERE {' AND '.join(where)}
        ORDER BY ts_utc DESC, strike ASC
    """
    return _fetch_all(conn, sql, tuple(params))


def candidates_at_ts(
    conn: sqlite3.Connection,
    *,
    event_ticker: str,
    ts_utc: str,
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM candidates
        WHERE event_ticker = ? AND ts_utc = ? AND (? IS NULL OR run_id = ?)
        ORDER BY strike ASC, side ASC
        """,
        (str(event_ticker), str(ts_utc), str(run_id) if run_id else None, str(run_id) if run_id else None),
    )


def candidate_timestamps(
    conn: sqlite3.Connection,
    *,
    event_ticker: str,
    run_id: Optional[str] = None,
    limit: int = 200,
) -> List[str]:
    rows = _fetch_all(
        conn,
        """
        SELECT ts_utc
        FROM candidates
        WHERE event_ticker = ?
          AND (? IS NULL OR run_id = ?)
        GROUP BY ts_utc
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (str(event_ticker), str(run_id) if run_id else None, str(run_id) if run_id else None, int(limit)),
    )
    return [str(r["ts_utc"]) for r in rows if r.get("ts_utc")]


def latest_candidate_timestamp(conn: sqlite3.Connection, *, event_ticker: str, run_id: Optional[str] = None) -> Optional[str]:
    row = conn.execute(
        """
        SELECT MAX(ts_utc) AS ts
        FROM candidates
        WHERE event_ticker = ?
          AND (? IS NULL OR run_id = ?)
        """,
        (str(event_ticker), str(run_id) if run_id else None, str(run_id) if run_id else None),
    ).fetchone()
    if not row:
        return None
    return row["ts"]


def candidates_latest_n(
    conn: sqlite3.Connection,
    *,
    event_ticker: str,
    run_id: Optional[str] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM candidates
        WHERE event_ticker = ?
          AND (? IS NULL OR run_id = ?)
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (str(event_ticker), str(run_id) if run_id else None, str(run_id) if run_id else None, int(limit)),
    )


def open_orders(conn: sqlite3.Connection, *, minutes: int = 720) -> List[Dict[str, Any]]:
    # Best-effort "open" inference: latest order_event status in recent window not canceled/filled/executed.
    return _fetch_all(
        conn,
        """
        WITH latest AS (
          SELECT
            order_id,
            MAX(ts_utc) AS last_ts
          FROM order_events
          WHERE ts_utc >= datetime('now', ?)
            AND order_id IS NOT NULL AND order_id <> ''
          GROUP BY order_id
        )
        SELECT oe.*
        FROM order_events oe
        JOIN latest l
          ON oe.order_id = l.order_id AND oe.ts_utc = l.last_ts
        WHERE COALESCE(oe.status, '') NOT IN ('canceled', 'cancelled', 'executed', 'filled', 'rejected')
        ORDER BY oe.ts_utc DESC
        """,
        (f"-{int(minutes)} minutes",),
    )


def open_orders_for_run(conn: sqlite3.Connection, *, run_id: str) -> List[Dict[str, Any]]:
    """
    Best-effort "open" inference within a specific run_id.

    Unlike `open_orders(...)`, this does not anchor to wall-clock time, which
    makes it usable for historical/backtest-like datasets.
    """
    return _fetch_all(
        conn,
        """
        WITH latest AS (
          SELECT
            order_id,
            MAX(ts_utc) AS last_ts
          FROM order_events
          WHERE run_id = ?
            AND order_id IS NOT NULL AND order_id <> ''
          GROUP BY order_id
        )
        SELECT oe.*
        FROM order_events oe
        JOIN latest l
          ON oe.order_id = l.order_id AND oe.ts_utc = l.last_ts
        WHERE COALESCE(oe.status, '') NOT IN ('canceled', 'cancelled', 'executed', 'filled', 'rejected')
        ORDER BY oe.ts_utc DESC
        """,
        (str(run_id),),
    )


def order_timeline(conn: sqlite3.Connection, order_id: str) -> List[Dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM order_events
        WHERE order_id = ?
        ORDER BY ts_utc ASC
        """,
        (str(order_id),),
    )


def fills_recent(conn: sqlite3.Connection, *, minutes: int = 1440, event_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    if event_ticker:
        return _fetch_all(
            conn,
            """
            SELECT *
            FROM fills
            WHERE ts_utc >= datetime('now', ?)
              AND event_ticker = ?
            ORDER BY ts_utc DESC
            """,
            (f"-{int(minutes)} minutes", str(event_ticker)),
        )
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM fills
        WHERE ts_utc >= datetime('now', ?)
        ORDER BY ts_utc DESC
        """,
        (f"-{int(minutes)} minutes",),
    )


def fills_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_ticker: Optional[str] = None,
    fill_kind: Optional[str] = None,
    limit: int = 20_000,
) -> List[Dict[str, Any]]:
    """
    Fills for a specific run_id (optionally filtered by event_ticker / fill_kind).
    Uses run-relative ordering rather than `datetime('now', ...)`.
    """
    where = ["run_id = ?"]
    params: List[Any] = [str(run_id)]
    if event_ticker:
        where.append("event_ticker = ?")
        params.append(str(event_ticker))
    if fill_kind:
        where.append("fill_kind = ?")
        params.append(str(fill_kind))
    sql = f"""
        SELECT *
        FROM fills
        WHERE {' AND '.join(where)}
        ORDER BY ts_utc DESC
        LIMIT ?
    """
    params.append(int(limit))
    return _fetch_all(conn, sql, tuple(params))


def pnl_series(conn: sqlite3.Connection, *, event_ticker: Optional[str] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    if event_ticker:
        return _fetch_all(
            conn,
            """
            SELECT ts_utc, event_ticker, market_ticker, realized, unrealized, total
            FROM pnl
            WHERE event_ticker = ?
            ORDER BY ts_utc ASC
            LIMIT ?
            """,
            (str(event_ticker), int(limit)),
        )
    return _fetch_all(
        conn,
        """
        SELECT ts_utc, event_ticker, market_ticker, realized, unrealized, total
        FROM pnl
        ORDER BY ts_utc ASC
        LIMIT ?
        """,
        (int(limit),),
    )


def pnl_series_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    event_ticker: Optional[str] = None,
    limit: int = 50_000,
) -> List[Dict[str, Any]]:
    where = ["run_id = ?"]
    params: List[Any] = [str(run_id)]
    if event_ticker:
        where.append("event_ticker = ?")
        params.append(str(event_ticker))
    sql = f"""
        SELECT ts_utc, run_id, event_ticker, market_ticker, realized, unrealized, total
        FROM pnl
        WHERE {' AND '.join(where)}
        ORDER BY ts_utc ASC
        LIMIT ?
    """
    params.append(int(limit))
    return _fetch_all(conn, sql, tuple(params))


def system_health_latest(conn: sqlite3.Connection, *, run_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    if run_id:
        return _fetch_all(
            conn,
            """
            SELECT *
            FROM system_health
            WHERE run_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (str(run_id), int(limit)),
        )
    return _fetch_all(
        conn,
        """
        SELECT *
        FROM system_health
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (int(limit),),
    )

