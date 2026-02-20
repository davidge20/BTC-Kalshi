import json
import tempfile
import unittest
from datetime import datetime, timezone

from kalshi_edge.strategy_config import StrategyConfig, config_hash
from kalshi_edge.trade_log import TradeLogger, resolve_trade_log_path


class TestTradeLogUpgrades(unittest.TestCase):
    def test_config_hash_stable_and_sensitive(self) -> None:
        a = StrategyConfig()
        b = StrategyConfig()
        ha = config_hash(a)
        hb = config_hash(b)
        self.assertEqual(ha, hb)

        # Changing a meaningful knob must change the hash.
        a.MIN_EV = 0.051
        self.assertNotEqual(ha, config_hash(a))

    def test_schema_validation_non_strict_annotates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = td + "/log.jsonl"
            log = TradeLogger(path, run_id="R1", base_fields={"strategy_name": "v2", "strategy_schema_version": "v2.2"})
            log.log("order_submit", {"market_ticker": "M1", "side": "yes"})  # missing count, price_cents

            with open(path, "r", encoding="utf-8") as f:
                line = f.readline().strip()
            rec = json.loads(line)
            self.assertEqual(rec["event"], "order_submit")
            self.assertEqual(rec["run_id"], "R1")
            self.assertEqual(rec["strategy_name"], "v2")
            self.assertIn("_schema_missing", rec)
            self.assertIn("count", rec["_schema_missing"])
            self.assertIn("price_cents", rec["_schema_missing"])

    def test_schema_validation_strict_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = td + "/log.jsonl"
            log = TradeLogger(
                path,
                run_id="R1",
                base_fields={"strategy_name": "v2", "strategy_schema_version": "v2.2"},
                strict_schema=True,
            )
            with self.assertRaises(ValueError):
                log.log("order_submit", {"market_ticker": "M1", "side": "yes"})

    def test_trade_log_dir_resolution(self) -> None:
        now = datetime(2026, 2, 19, 12, 0, 0, tzinfo=timezone.utc)
        p = resolve_trade_log_path(trade_log_file="trade_log.jsonl", trade_log_dir="logs/raw", run_id="abc", now_utc=now)
        self.assertEqual(p, "logs/raw/2026-02-19/abc.jsonl")
        p2 = resolve_trade_log_path(trade_log_file="trade_log.jsonl", trade_log_dir=None, run_id="abc", now_utc=now)
        self.assertEqual(p2, "trade_log.jsonl")

    def test_state_file_moves_under_logs_root(self) -> None:
        # Mirrors run.py logic for default state file when trade_log_dir is set.
        run_id = "abc"
        day = "2026-02-19"
        trade_log_dir = "logs/raw"
        base_dir = trade_log_dir.rstrip("/").rstrip("\\")
        import os

        logs_root = os.path.dirname(base_dir) or base_dir
        state_file = os.path.join(logs_root, "state", day, f"{run_id}.json")
        self.assertEqual(state_file, "logs/state/2026-02-19/abc.json")


if __name__ == "__main__":
    unittest.main()

