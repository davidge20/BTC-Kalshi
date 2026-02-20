from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FillDelta:
    delta_fill_count: int
    delta_cost_cents: int
    delta_fee_cents: int
    ts_utc: str

    @property
    def avg_price_cents(self) -> Optional[int]:
        if self.delta_fill_count <= 0:
            return None
        return int(round(self.delta_cost_cents / float(self.delta_fill_count)))

    @property
    def avg_fee_cents(self) -> Optional[int]:
        if self.delta_fill_count <= 0:
            return None
        return int(round(self.delta_fee_cents / float(self.delta_fill_count)))

