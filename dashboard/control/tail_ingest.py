from __future__ import annotations

import time
from pathlib import Path
from threading import Event

from dashboard.ingest.ingest_jsonl import ingest_jsonl


def tail_and_ingest(
    *,
    stop: Event,
    input_path: str,
    db_path: str,
    poll_seconds: float = 0.5,
    from_start: bool = False,
) -> None:
    """
    Tail a JSONL file and incrementally ingest newly appended data into SQLite.
    Designed for Streamlit background threads: stoppable via `stop`.
    """
    path = Path(input_path)
    pos = 0
    if path.exists() and (not from_start):
        try:
            pos = path.stat().st_size
        except Exception:
            pos = 0

    tmp = path.parent / (path.name + ".dashboard_tail.tmp.jsonl")

    while not stop.is_set():
        if not path.exists():
            time.sleep(poll_seconds)
            continue

        try:
            size = path.stat().st_size
        except Exception:
            time.sleep(poll_seconds)
            continue

        if size < pos:
            pos = 0

        if size == pos:
            time.sleep(poll_seconds)
            continue

        try:
            with path.open("r", encoding="utf-8") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except Exception:
            time.sleep(poll_seconds)
            continue

        try:
            tmp.write_text(chunk, encoding="utf-8")
            ingest_jsonl(input_path=str(tmp), db_path=str(db_path))
        except Exception:
            # keep loop resilient; UI will show trader logs anyway
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

        time.sleep(poll_seconds)

