from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


def _expand_path(p: str) -> str:
    return str(Path(p).expanduser())


def _truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class DashboardConfig:
    db_path: str
    default_log_path: Optional[str]
    default_logs_dir: Optional[str]
    refresh_seconds: float
    timezone: str


def load_dashboard_config(path: Optional[str] = None) -> DashboardConfig:
    """
    Resolve dashboard config from:
    - explicit `path`
    - env var `KALSHI_EDGE_DASHBOARD_CONFIG` (JSON)
    - defaults

    We keep this JSON-only to avoid extra deps; YAML/TOML can be added later.
    """
    env_path = os.environ.get("KALSHI_EDGE_DASHBOARD_CONFIG")
    cfg_path = path or env_path

    raw: Dict[str, Any] = {}
    if cfg_path:
        p = Path(_expand_path(cfg_path))
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8") or "{}")

    # Defaults: store DB under repo-local `.dashboard/`
    default_db = raw.get("db_path") or os.environ.get("KALSHI_EDGE_DASHBOARD_DB") or ".dashboard/kalshi_edge_dashboard.sqlite"
    default_log_path = raw.get("log_path") or os.environ.get("KALSHI_EDGE_DASHBOARD_LOG")
    default_logs_dir = raw.get("logs_dir") or os.environ.get("KALSHI_EDGE_DASHBOARD_LOGS_DIR") or "logs"

    refresh_seconds = float(raw.get("refresh_seconds") or os.environ.get("KALSHI_EDGE_DASHBOARD_REFRESH_SECONDS") or 5.0)
    tz = str(raw.get("timezone") or os.environ.get("KALSHI_EDGE_DASHBOARD_TZ") or "UTC")

    # Allow disabling auto-refresh by setting 0
    if refresh_seconds < 0:
        refresh_seconds = 0.0

    return DashboardConfig(
        db_path=_expand_path(str(default_db)),
        default_log_path=_expand_path(default_log_path) if default_log_path else None,
        default_logs_dir=_expand_path(default_logs_dir) if default_logs_dir else None,
        refresh_seconds=refresh_seconds,
        timezone=tz,
    )


def ensure_parent_dir(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

