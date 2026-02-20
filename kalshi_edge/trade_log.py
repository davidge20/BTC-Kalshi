# kalshi_edge/trade_log.py
import json
import os
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from kalshi_edge.trade_log_schema import EVENT_SCHEMAS, LOG_SCHEMA_VERSION


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeLogger:
    """
    Append-only JSONL logger.
    Each call writes one line of JSON to `path`.
    """

    def __init__(
        self,
        path: str,
        *,
        run_id: Optional[str] = None,
        base_fields: Optional[Dict[str, Any]] = None,
        strict_schema: bool = False,
    ):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        self.run_id = str(run_id) if run_id is not None else None
        self.base_fields: Dict[str, Any] = dict(base_fields or {})
        self.strict_schema = bool(strict_schema)
        self._bot_start_logged = False

        # Prevent accidental overrides of core fields.
        self.base_fields.pop("ts_utc", None)
        self.base_fields.pop("event", None)
        if self.run_id is not None:
            self.base_fields["run_id"] = self.run_id

    def log(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        if event == "bot_start":
            if self._bot_start_logged:
                if self.strict_schema:
                    raise ValueError("bot_start already logged for this TradeLogger instance")
                return
            self._bot_start_logged = True

        rec: Dict[str, Any] = {"ts_utc": utc_ts(), "event": str(event)}
        if self.base_fields:
            rec.update(self.base_fields)
        if data:
            rec.update(data)

        self._validate_and_annotate(rec)
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _validate_and_annotate(self, rec: Dict[str, Any]) -> None:
        event = str(rec.get("event") or "")
        spec = EVENT_SCHEMAS.get(event)
        if spec is None:
            return

        missing = [k for k in spec.required_keys if k not in rec or rec.get(k) is None]
        if not missing:
            return

        if self.strict_schema:
            raise ValueError(f"Trade log schema missing required keys for event={event}: {missing}")

        rec["_schema_version"] = LOG_SCHEMA_VERSION
        rec["_schema_event"] = event
        rec["_schema_missing"] = missing


def stable_json_hash(obj: Any) -> str:
    """
    Deterministic sha256 hash of JSON-serializable data.
    Uses sorted keys and compact separators.
    """
    b = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def resolve_trade_log_path(
    *,
    trade_log_file: str,
    trade_log_dir: Optional[str],
    run_id: str,
    now_utc: Optional[datetime] = None,
) -> str:
    """
    Resolve the actual JSONL path to write to.

    - If `trade_log_dir` is provided, logs go to: <dir>/<YYYY-MM-DD>/<run_id>.jsonl
    - Otherwise, `trade_log_file` is used as-is.
    """
    if not trade_log_dir:
        return str(trade_log_file)
    dt = now_utc or datetime.now(timezone.utc)
    day = dt.strftime("%Y-%m-%d")
    return os.path.join(str(trade_log_dir), day, f"{run_id}.jsonl")
