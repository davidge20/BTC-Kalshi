import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from kalshi_edge.strategy_config import PaperConfig, StrategyConfig, config_hash
from kalshi_edge.trader_engine import SCHEMA, Trader


class _HttpNoop:
    def get_json(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
        raise RuntimeError("unexpected HTTP in unit test")

    def post_json(self, url: str, json_body: Optional[dict] = None, headers: Optional[dict] = None) -> Dict[str, Any]:
        raise RuntimeError("unexpected HTTP in unit test")

    def request_json(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> Dict[str, Any]:
        raise RuntimeError("unexpected HTTP in unit test")


@dataclass
class _OB:
    ybid: Optional[int]
    yqty: Optional[float]
    nbid: Optional[int]
    nqty: Optional[float]
    ybuy: Optional[int]
    nbuy: Optional[int]
    spread_y: Optional[int]
    spread_n: Optional[int]


@dataclass
class _Row:
    ticker: str
    strike: float
    subtitle: str
    p_model: float
    ob: _OB


@dataclass
class _MS:
    spot: float = 60_000.0
    sigma_implied: float = 0.8
    sigma_realized: float = 0.7
    sigma_blend: float = 0.75
    confidence: str = "test"
    note: str = ""


@dataclass
class _Res:
    event_ticker: str
    minutes_left: float
    market_state: _MS
    rows: List[_Row]


def _read_events(path: str) -> List[str]:
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(str(json.loads(line).get("event")))
    return out


class TestTraderSmoke(unittest.TestCase):
    def test_on_tick_dry_run_taker_enters_and_persists_state(self) -> None:
        cfg = StrategyConfig(
            ORDER_MODE="taker_only",
            POST_ONLY=True,
            MIN_EV=0.05,
            FEE_CENTS=1,
            ORDER_SIZE=1,
            MAX_CONTRACTS_PER_MARKET=1,
            MIN_TOP_SIZE=0.0,
            SPREAD_MAX_CENTS=999,
            MAX_COST_PER_EVENT=10_000.0,
            MAX_COST_PER_MARKET=10_000.0,
            MAX_POSITIONS_PER_EVENT=999,
            paper=PaperConfig(simulate_maker_fills=False),
        )

        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "state.json")
            trade_log_file = os.path.join(td, "events.jsonl")

            t = Trader(
                http=_HttpNoop(),
                auth=None,
                kalshi_base_url="https://example.invalid",
                state_file=state_file,
                trade_log_file=trade_log_file,
                dry_run=True,
                config=cfg,
                run_id="R1",
                base_log_fields={
                    "strategy_name": "trader",
                    "strategy_schema_version": str(SCHEMA),
                    "config_hash": config_hash(cfg),
                    "dry_run": True,
                    "paper": True,
                    "live": False,
                },
            )

            row = _Row(
                ticker="TEST-MKT",
                strike=60_000.0,
                subtitle="test",
                p_model=0.90,
                ob=_OB(
                    ybid=50,
                    yqty=10.0,
                    nbid=48,
                    nqty=10.0,
                    ybuy=52,
                    nbuy=52,
                    spread_y=2,
                    spread_n=4,
                ),
            )
            res = _Res(event_ticker="TEST-EVT", minutes_left=10.0, market_state=_MS(), rows=[row])

            t.on_tick(res)  # should not raise

            self.assertIn("TEST-MKT", t.open_positions)
            pos = t.open_positions["TEST-MKT"]
            self.assertEqual(pos.get("side"), "yes")
            self.assertEqual(int(pos.get("total_count") or 0), 1)
            self.assertTrue(os.path.exists(state_file))

            events = _read_events(trade_log_file)
            self.assertIn("tick_summary", events)
            self.assertIn("decision", events)
            self.assertIn("order_submit", events)
            self.assertIn("entry_filled", events)

    def test_on_tick_no_candidate_does_not_write_state(self) -> None:
        cfg = StrategyConfig(
            ORDER_MODE="taker_only",
            MIN_EV=0.05,
            FEE_CENTS=1,
            ORDER_SIZE=1,
            MAX_CONTRACTS_PER_MARKET=1,
            MIN_TOP_SIZE=0.0,
            SPREAD_MAX_CENTS=999,
            paper=PaperConfig(simulate_maker_fills=False),
        )

        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "state.json")
            trade_log_file = os.path.join(td, "events.jsonl")

            t = Trader(
                http=_HttpNoop(),
                auth=None,
                kalshi_base_url="https://example.invalid",
                state_file=state_file,
                trade_log_file=trade_log_file,
                dry_run=True,
                config=cfg,
                run_id="R1",
                base_log_fields={
                    "strategy_name": "trader",
                    "strategy_schema_version": str(SCHEMA),
                    "config_hash": config_hash(cfg),
                    "dry_run": True,
                    "paper": True,
                    "live": False,
                },
            )

            row = _Row(
                ticker="TEST-MKT",
                strike=60_000.0,
                subtitle="test",
                p_model=0.50,
                ob=_OB(
                    ybid=50,
                    yqty=10.0,
                    nbid=50,
                    nqty=10.0,
                    ybuy=None,
                    nbuy=None,
                    spread_y=None,
                    spread_n=None,
                ),
            )
            res = _Res(event_ticker="TEST-EVT", minutes_left=10.0, market_state=_MS(), rows=[row])

            t.on_tick(res)

            self.assertEqual(t.open_positions, {})
            self.assertFalse(os.path.exists(state_file))

            events = _read_events(trade_log_file)
            self.assertIn("tick_summary", events)
            self.assertNotIn("order_submit", events)


if __name__ == "__main__":
    unittest.main()

