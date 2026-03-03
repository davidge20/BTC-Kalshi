import math
import statistics
import unittest

from kalshi_edge.backtesting.backtest_engine import (
    annualized_realized_vol_from_closes,
    dollars_to_cents,
    derive_no_quotes,
    edge_at_price,
    max_acceptable_price_cents,
    rolling_annualized_realized_vol,
)
from kalshi_edge.constants import MINUTES_PER_YEAR


class TestBacktestParsing(unittest.TestCase):
    def test_dollars_to_cents(self) -> None:
        self.assertEqual(dollars_to_cents("0.52"), 52)
        self.assertEqual(dollars_to_cents("$0.41"), 41)
        self.assertEqual(dollars_to_cents("52"), 52)
        self.assertEqual(dollars_to_cents(52), 52)
        self.assertIsNone(dollars_to_cents(-1))
        self.assertIsNone(dollars_to_cents("bad"))

    def test_derive_no_quotes_from_yes_quotes(self) -> None:
        nbid, nask = derive_no_quotes(yes_bid_cents=47, yes_ask_cents=49)
        self.assertEqual(nbid, 51)
        self.assertEqual(nask, 53)


class TestRealizedVol(unittest.TestCase):
    def test_annualized_realized_vol(self) -> None:
        closes = [100.0, 101.0, 100.0, 102.0]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        expected = statistics.pstdev(rets) * math.sqrt(MINUTES_PER_YEAR)
        got = annualized_realized_vol_from_closes(closes)
        self.assertAlmostEqual(got, expected, places=12)

    def test_rolling_window_annualization(self) -> None:
        closes = [100.0, 101.0, 99.5, 100.5, 102.0]
        window = 3
        series = rolling_annualized_realized_vol(closes, window)
        self.assertEqual(len(series), len(closes))
        last_expected = annualized_realized_vol_from_closes(closes[-window:])
        self.assertAlmostEqual(series[-1], last_expected, places=12)


class TestPricingLogic(unittest.TestCase):
    def test_max_acceptable_price_and_edge_consistency(self) -> None:
        p_win = 0.60
        min_ev = 0.05
        fee = 1
        max_px = max_acceptable_price_cents(p_win=p_win, min_ev=min_ev, fee_buffer_cents=fee)

        ev_at_max = edge_at_price(p_win=p_win, price_cents=max_px, fee_cents=fee)
        ev_above = edge_at_price(p_win=p_win, price_cents=max_px + 1, fee_cents=fee)

        self.assertGreaterEqual(ev_at_max, min_ev - 0.01)
        self.assertLess(ev_above, min_ev)


if __name__ == "__main__":
    unittest.main()
