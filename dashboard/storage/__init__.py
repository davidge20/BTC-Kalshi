from __future__ import annotations

from pathlib import Path
from typing import Optional

from dashboard.storage.db import connect, migrate


def default_schema_path() -> str:
    return str(Path(__file__).resolve().parent / "schema.sql")


def open_db(db_path: str, *, schema_sql_path: Optional[str] = None):
    conn = connect(db_path)
    migrate(conn, schema_sql_path or default_schema_path())
    return conn

