"""
formatting.py

Tiny formatting helpers for nicer terminal output.
"""

from __future__ import annotations
from typing import Optional


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_cents(x: Optional[int]) -> str:
    return "-" if x is None else str(int(x))


def fmt_float(x: Optional[float], nd: int = 3) -> str:
    return "-" if x is None else f"{x:.{nd}f}"