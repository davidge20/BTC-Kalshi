"""
formatting.py

Tiny helpers for nicer terminal output + time parsing.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def parse_iso8601(s: str) -> datetime:
    """Parse Kalshi ISO-8601 strings.

    Kalshi commonly emits `...Z` (UTC designator). Python's `fromisoformat()`
    doesn't accept `Z`, so we normalize to `+00:00`.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_cents(x: Optional[int]) -> str:
    return "-" if x is None else str(int(x))


def fmt_float(x: Optional[float], nd: int = 3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"