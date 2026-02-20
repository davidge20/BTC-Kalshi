import random
import unittest
from datetime import datetime, timedelta, timezone

from kalshi_edge.paper_fill_sim import PaperFillSimulator
from kalshi_edge.strategy_config import PaperConfig


class TestPaperFillSimulator(unittest.TestCase):
    def test_maker_resting_fills_after_min_top_time(self) -> None:
        cfg = PaperConfig(
            simulate_maker_fills=True,
            tick_seconds=1,
            min_top_time_seconds=3,
            fill_prob_per_tick=1.0,  # deterministic: always fill once eligible
            partial_fill=True,
            max_fill_per_tick=2,
            slippage_cents=0,
            seed=12345,
        )
        rng = random.Random(cfg.seed)
        sim = PaperFillSimulator(cfg, rng, fee_cents_per_contract=1)

        tracked = {
            "order_id": "O1",
            "market_ticker": "M1",
            "event_ticker": "E1",
            "side": "yes",
            "action": "buy",
            "source": "maker",
            "status": "resting",
            "price_cents": 50,
            "count": 5,
            "fill_count": 0,
            "remaining_count": 5,
            "last_fill_cost_cents": 0,
            "last_fee_paid_cents": 0,
        }

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        # At-top immediately, but should not fill until >= min_top_time_seconds.
        for i in range(3):
            ts = (base + timedelta(seconds=i)).isoformat()
            sim.update_book("M1", best_bid_cents=50, best_ask_cents=52, ts_utc=ts)
            d = sim.maybe_fill(tracked, ts)
            self.assertIsNone(d)
            self.assertEqual(tracked["fill_count"], 0)
            self.assertEqual(tracked["remaining_count"], 5)

        # Eligible at t=3s -> must fill.
        ts = (base + timedelta(seconds=3)).isoformat()
        sim.update_book("M1", best_bid_cents=50, best_ask_cents=52, ts_utc=ts)
        d = sim.maybe_fill(tracked, ts)
        self.assertIsNotNone(d)
        assert d is not None
        self.assertGreaterEqual(d.delta_fill_count, 1)
        self.assertLessEqual(d.delta_fill_count, 2)
        self.assertEqual(d.avg_price_cents, 50)
        self.assertEqual(d.avg_fee_cents, 1)
        self.assertEqual(tracked["fill_count"], d.delta_fill_count)
        self.assertEqual(tracked["remaining_count"], 5 - d.delta_fill_count)
        self.assertEqual(tracked["last_fill_cost_cents"], d.delta_cost_cents)
        self.assertEqual(tracked["last_fee_paid_cents"], d.delta_fee_cents)

        # Continue ticking; total fills must never exceed original remaining.
        for j in range(4, 50):
            if tracked["remaining_count"] <= 0:
                break
            tsj = (base + timedelta(seconds=j)).isoformat()
            sim.update_book("M1", best_bid_cents=50, best_ask_cents=52, ts_utc=tsj)
            _ = sim.maybe_fill(tracked, tsj)
            self.assertLessEqual(tracked["fill_count"], 5)
            self.assertGreaterEqual(tracked["remaining_count"], 0)

        self.assertEqual(tracked["fill_count"], 5)
        self.assertEqual(tracked["remaining_count"], 0)
        self.assertEqual(tracked["status"], "executed")


if __name__ == "__main__":
    unittest.main()

