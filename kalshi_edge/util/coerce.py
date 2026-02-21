"""
Safe type-coercion helpers used across execution and state management.
"""

from __future__ import annotations

from typing import Any


def as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or isinstance(x, bool):
            return default
        return int(x)
    except Exception:
        return default


def as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return default
        return float(x)
    except Exception:
        return default
