import tempfile
import unittest
import json

from dashboard.ingest.ingest_jsonl import ingest_jsonl
from dashboard.storage import open_db
from dashboard.storage import queries as q


class TestDashboardIngest(unittest.TestCase):
    def test_demo_ingest_populates_candidates_and_health(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = td + "/dash.sqlite"
            jsonl_path = td + "/events.jsonl"

            # Minimal synthetic log: one event with a couple of ladder rows + one tick summary.
            event_ticker = "KXBTCD-TEST-2026"
            run_id = "test-run"
            rows = [
                {
                    "ts_utc": "2026-03-04T00:00:00Z",
                    "event": "tick_summary",
                    "run_id": run_id,
                    "event_ticker": event_ticker,
                    "minutes_left": 30.0,
                    "spot": 68500.0,
                    "sigma_blend": 0.8,
                },
                {
                    "ts_utc": "2026-03-04T00:00:00Z",
                    "event": "candidate",
                    "run_id": run_id,
                    "event_ticker": event_ticker,
                    "market_ticker": f"{event_ticker}-T68500.00",
                    "side": "yes",
                    "price_cents": 52,
                    "fee_cents": 1,
                    "p_yes": 0.56,
                    "implied_q_yes": 0.52,
                    "edge_pp": 0.03,
                    "ev": 0.03,
                    "spread_cents": 2,
                    "top_size": 100.0,
                    "strike": 68500.0,
                    "minutes_left": 30.0,
                    "spot": 68500.0,
                    "sigma_blend": 0.8,
                    "source": "test",
                },
                {
                    "ts_utc": "2026-03-04T00:00:00Z",
                    "event": "candidate",
                    "run_id": run_id,
                    "event_ticker": event_ticker,
                    "market_ticker": f"{event_ticker}-T69000.00",
                    "side": "yes",
                    "price_cents": 44,
                    "fee_cents": 1,
                    "p_yes": 0.48,
                    "implied_q_yes": 0.44,
                    "edge_pp": 0.03,
                    "ev": 0.03,
                    "spread_cents": 3,
                    "top_size": 120.0,
                    "strike": 69000.0,
                    "minutes_left": 30.0,
                    "spot": 68500.0,
                    "sigma_blend": 0.8,
                    "source": "test",
                },
            ]
            with open(jsonl_path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

            n = ingest_jsonl(input_path=jsonl_path, db_path=db_path)
            self.assertGreaterEqual(n, 3)

            conn = open_db(db_path)
            try:
                events = q.list_event_tickers(conn)
                self.assertIn(event_ticker, events)
                ts = q.latest_candidate_timestamp(conn, event_ticker=event_ticker)
                self.assertIsNotNone(ts)
                cand = q.candidates_at_ts(conn, event_ticker=event_ticker, ts_utc=str(ts))
                self.assertGreaterEqual(len(cand), 2)
                health = q.system_health_latest(conn, limit=50)
                self.assertGreaterEqual(len(health), 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

