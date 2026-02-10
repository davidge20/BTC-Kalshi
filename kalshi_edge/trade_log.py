# kalshi_edge/trade_log.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeLogger:
    """
    Append-only JSONL logger.
    Each call writes one line of JSON to `path`.
    """

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

    def log(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        rec: Dict[str, Any] = {"ts_utc": utc_ts(), "event": event}
        if data:
            rec.update(data)
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
