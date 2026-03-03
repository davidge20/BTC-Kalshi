"""
Simple gzip JSON file cache for backtests.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any, Optional


def _safe_part(s: str) -> str:
    out = str(s).strip().replace("/", "_")
    return out.replace("\\", "_")


class FileCache:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = str(base_dir)

    def _path(self, *parts: str) -> str:
        return os.path.join(self.base_dir, *parts)

    def _ensure_parent(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def read_json_gz(self, path: str) -> Optional[Any]:
        if not os.path.exists(path):
            return None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)

    def write_json_gz(self, path: str, payload: Any) -> None:
        self._ensure_parent(path)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)

    def kalshi_markets_path(self, event_ticker: str) -> str:
        return self._path("kalshi_markets", f"{_safe_part(event_ticker)}.json.gz")

    def kalshi_candles_path(self, market_ticker: str, start_ts: int, end_ts: int) -> str:
        return self._path(
            "kalshi_candles",
            _safe_part(market_ticker),
            f"{int(start_ts)}-{int(end_ts)}-1m-v2.json.gz",
        )

    def coinbase_candles_path(self, product: str, start_ts: int, end_ts: int) -> str:
        return self._path(
            "coinbase",
            _safe_part(product),
            f"{int(start_ts)}-{int(end_ts)}-1m.json.gz",
        )
