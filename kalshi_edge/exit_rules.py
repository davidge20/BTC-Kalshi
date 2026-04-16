from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExitMarketSnapshot:
    side: str
    p_yes: float
    minutes_left: float
    yes_bid_cents: Optional[int]
    yes_ask_cents: Optional[int]
    no_bid_cents: Optional[int]
    no_ask_cents: Optional[int]


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    bid_cents: Optional[int]
    ask_cents: Optional[int]
    mid_cents: Optional[float]
    p_win_now: float
    avg_entry_cost_cents: float
    edge_now_pp: float


def avg_entry_cost_cents(*, total_count: int, total_cost_dollars: float) -> Optional[float]:
    if int(total_count) <= 0:
        return None
    return (float(total_cost_dollars) * 100.0) / float(total_count)


def side_quotes(snapshot: ExitMarketSnapshot) -> tuple[Optional[int], Optional[int]]:
    side = str(snapshot.side).lower()
    if side == "yes":
        return snapshot.yes_bid_cents, snapshot.yes_ask_cents
    return snapshot.no_bid_cents, snapshot.no_ask_cents


def side_win_probability(snapshot: ExitMarketSnapshot) -> float:
    side = str(snapshot.side).lower()
    p_yes = float(snapshot.p_yes)
    return p_yes if side == "yes" else (1.0 - p_yes)


def side_mid_cents(snapshot: ExitMarketSnapshot) -> Optional[float]:
    bid_cents, ask_cents = side_quotes(snapshot)
    if bid_cents is None or ask_cents is None:
        return None
    return (float(bid_cents) + float(ask_cents)) / 2.0


def should_pause_new_entries(*, minutes_left: float, exit_minutes_left: float) -> bool:
    return float(minutes_left) <= float(exit_minutes_left)


def evaluate_exit(
    *,
    snapshot: ExitMarketSnapshot,
    total_count: int,
    total_cost_dollars: float,
    take_profit_mid_cents: Optional[int],
    exit_minutes_left: float,
    signal_exit_enabled: bool,
    signal_exit_min_edge_pp: float,
) -> Optional[ExitDecision]:
    avg_cost_cents = avg_entry_cost_cents(total_count=int(total_count), total_cost_dollars=float(total_cost_dollars))
    if avg_cost_cents is None:
        return None

    bid_cents, ask_cents = side_quotes(snapshot)
    mid_cents = side_mid_cents(snapshot)
    p_win_now = side_win_probability(snapshot)
    edge_now_pp = float(p_win_now) - (float(avg_cost_cents) / 100.0)

    if float(snapshot.minutes_left) <= float(exit_minutes_left):
        return ExitDecision(
            reason="minutes_left_exit",
            bid_cents=bid_cents,
            ask_cents=ask_cents,
            mid_cents=mid_cents,
            p_win_now=float(p_win_now),
            avg_entry_cost_cents=float(avg_cost_cents),
            edge_now_pp=float(edge_now_pp),
        )

    if bool(signal_exit_enabled) and float(edge_now_pp) <= float(signal_exit_min_edge_pp):
        return ExitDecision(
            reason="signal_reversal",
            bid_cents=bid_cents,
            ask_cents=ask_cents,
            mid_cents=mid_cents,
            p_win_now=float(p_win_now),
            avg_entry_cost_cents=float(avg_cost_cents),
            edge_now_pp=float(edge_now_pp),
        )

    if take_profit_mid_cents is not None and mid_cents is not None and float(mid_cents) >= float(take_profit_mid_cents):
        return ExitDecision(
            reason="take_profit_mid",
            bid_cents=bid_cents,
            ask_cents=ask_cents,
            mid_cents=mid_cents,
            p_win_now=float(p_win_now),
            avg_entry_cost_cents=float(avg_cost_cents),
            edge_now_pp=float(edge_now_pp),
        )

    return None
