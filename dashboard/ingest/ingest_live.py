from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional

from dashboard.config import ensure_parent_dir, load_dashboard_config
from dashboard.ingest.ingest_jsonl import ingest_jsonl


def ingest_live(*, input_path: str, db_path: str, poll_seconds: float = 0.25, from_start: bool = False) -> None:
    """
    Best-effort tail ingestion for a JSONL file being appended to.
    This is intentionally simple (Phase 2): it periodically checks for new bytes
    and ingests newly appended lines.
    """
    path = Path(input_path)
    pos = 0
    if path.exists() and (not from_start):
        pos = path.stat().st_size

    print(f"[dashboard] tailing {input_path} -> {db_path} (from_start={from_start})")

    while True:
        if not path.exists():
            time.sleep(poll_seconds)
            continue

        size = path.stat().st_size
        if size < pos:
            # log rotated/truncated
            pos = 0

        if size == pos:
            time.sleep(poll_seconds)
            continue

        # Read new region to a temp buffer file-like and ingest by writing to a temp file
        with path.open("r", encoding="utf-8") as f:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()

        # Write chunk to a temporary file to reuse ingest_jsonl line iterator
        tmp = path.parent / (path.name + ".dashboard_tail.tmp.jsonl")
        tmp.write_text(chunk, encoding="utf-8")
        try:
            ingest_jsonl(input_path=str(tmp), db_path=db_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="dashboard.ingest_live", description="Tail a kalshi_edge JSONL log and ingest into SQLite.")
    ap.add_argument("--input", required=True, type=str, help="Path to JSONL file to tail")
    ap.add_argument("--db", required=False, type=str, default=None, help="SQLite DB path (default from dashboard config)")
    ap.add_argument("--config", required=False, type=str, default=None, help="Dashboard config JSON path")
    ap.add_argument("--poll-seconds", required=False, type=float, default=0.25)
    ap.add_argument("--from-start", action="store_true", help="If set, ingest from start of file (otherwise tail from end).")
    args = ap.parse_args()

    cfg = load_dashboard_config(args.config)
    db_path = args.db or cfg.db_path
    ensure_parent_dir(db_path)

    ingest_live(
        input_path=str(args.input),
        db_path=str(db_path),
        poll_seconds=float(args.poll_seconds),
        from_start=bool(args.from_start),
    )


if __name__ == "__main__":
    main()

