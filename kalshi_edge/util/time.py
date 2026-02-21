"""
Canonical time helpers.

Every module that needs UTC timestamps or ISO-8601 parsing should import from
here instead of re-defining its own version.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def utc_ts() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return utc_now().isoformat()


def parse_iso8601(s: str) -> datetime:
    """Parse ISO-8601 with Kalshi-style trailing 'Z'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse an optional ISO-8601 string, returning None on failure."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return parse_iso8601(ts)
    except Exception:
        return None


def secs_since(ts: Optional[str]) -> Optional[float]:
    """Seconds elapsed since the given ISO-8601 timestamp, or None."""
    dt = parse_ts(ts)
    if dt is None:
        return None
    return max(0.0, (utc_now() - dt).total_seconds())
