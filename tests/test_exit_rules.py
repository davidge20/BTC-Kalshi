import unittest

from kalshi_edge.exit_rules import ExitMarketSnapshot, evaluate_exit, should_pause_new_entries


class TestExitRules(unittest.TestCase):
    def test_take_profit_mid_exit(self) -> None:
        decision = evaluate_exit(
            snapshot=ExitMarketSnapshot(
                side="yes",
                p_yes=0.88,
                minutes_left=12.0,
                yes_bid_cents=90,
                yes_ask_cents=92,
                no_bid_cents=8,
                no_ask_cents=10,
            ),
            total_count=1,
            total_cost_dollars=0.31,
            take_profit_mid_cents=90,
            exit_minutes_left=5.0,
            signal_exit_enabled=True,
            signal_exit_min_edge_pp=0.0,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "take_profit_mid")
        self.assertEqual(decision.bid_cents, 90)

    def test_signal_reversal_exit(self) -> None:
        decision = evaluate_exit(
            snapshot=ExitMarketSnapshot(
                side="yes",
                p_yes=0.20,
                minutes_left=12.0,
                yes_bid_cents=18,
                yes_ask_cents=21,
                no_bid_cents=79,
                no_ask_cents=82,
            ),
            total_count=1,
            total_cost_dollars=0.31,
            take_profit_mid_cents=90,
            exit_minutes_left=5.0,
            signal_exit_enabled=True,
            signal_exit_min_edge_pp=0.0,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.reason, "signal_reversal")

    def test_minutes_left_pause(self) -> None:
        self.assertTrue(should_pause_new_entries(minutes_left=4.0, exit_minutes_left=5.0))
        self.assertFalse(should_pause_new_entries(minutes_left=6.0, exit_minutes_left=5.0))


if __name__ == "__main__":
    unittest.main()
