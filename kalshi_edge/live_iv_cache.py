"""
live_iv_cache.py — append-only cache for live Deribit ATM implied-vol snapshots.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def default_live_iv_cache_path() -> str:
    return os.path.join("data", "live_iv", "deribit_btc_atm_iv.jsonl")


@dataclass(frozen=True)
class LiveIVSnapshot:
    ts_utc: str
    ts_s: int
    spot: float
    sigma_implied: float
    iv_band_pct: float
    note: str

    def to_record(self) -> Dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "ts_s": int(self.ts_s),
            "spot": float(self.spot),
            "sigma_implied": float(self.sigma_implied),
            "iv_band_pct": float(self.iv_band_pct),
            "note": str(self.note),
        }


def append_live_iv_snapshot(path: str, snapshot: LiveIVSnapshot) -> None:
    _ensure_parent(path)
    line = json.dumps(snapshot.to_record(), sort_keys=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_live_iv_snapshots(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def snapshot_now(*, spot: float, sigma_implied: float, iv_band_pct: float, note: str) -> LiveIVSnapshot:
    now = datetime.now(timezone.utc)
    return LiveIVSnapshot(
        ts_utc=now.isoformat().replace("+00:00", "Z"),
        ts_s=int(now.timestamp()),
        spot=float(spot),
        sigma_implied=float(sigma_implied),
        iv_band_pct=float(iv_band_pct),
        note=str(note),
    )
