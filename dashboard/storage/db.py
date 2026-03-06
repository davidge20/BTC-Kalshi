from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


SCHEMA_VERSION = 2


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    # `timeout` causes sqlite to wait for locks instead of failing fast.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version;").fetchone()
    return int(row[0]) if row is not None else 0


def _set_user_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute(f"PRAGMA user_version={int(v)};")


def migrate(conn: sqlite3.Connection, schema_sql_path: str) -> None:
    """
    Minimal migration strategy using PRAGMA user_version and idempotent schema SQL.
    Safe to call on every startup.
    """
    current = _get_user_version(conn)
    if current >= SCHEMA_VERSION:
        return

    schema_sql = Path(schema_sql_path).read_text(encoding="utf-8")
    with conn:
        conn.executescript(schema_sql)
        _set_user_version(conn, SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def insert_event(
    conn: sqlite3.Connection,
    *,
    ts_utc: str,
    event: str,
    payload: Dict[str, Any],
) -> int:
    run_id = payload.get("run_id")
    event_ticker = payload.get("event_ticker")
    market_ticker = payload.get("market_ticker") or payload.get("ticker")
    order_id = payload.get("order_id")
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    cur = conn.execute(
        """
        INSERT INTO events(ts_utc, event, run_id, event_ticker, market_ticker, order_id, payload_json)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (ts_utc, str(event), run_id, event_ticker, market_ticker, order_id, payload_json),
    )
    return int(cur.lastrowid)


def insert_many(conn: sqlite3.Connection, sql: str, rows: Iterable[Tuple[Any, ...]]) -> None:
    conn.executemany(sql, list(rows))

