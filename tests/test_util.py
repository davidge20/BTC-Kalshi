"""Tests for kalshi_edge.util.time and kalshi_edge.util.coerce."""

import unittest
from datetime import datetime, timezone

from kalshi_edge.util.time import utc_now, utc_ts, parse_iso8601, parse_ts, secs_since
from kalshi_edge.util.coerce import as_int, as_float


class TestTimeHelpers(unittest.TestCase):
    def test_utc_now_is_aware(self) -> None:
        dt = utc_now()
        self.assertIsNotNone(dt.tzinfo)

    def test_utc_ts_parses_back(self) -> None:
        ts = utc_ts()
        dt = parse_iso8601(ts)
        self.assertIsInstance(dt, datetime)

    def test_parse_iso8601_z_suffix(self) -> None:
        dt = parse_iso8601("2026-02-20T12:00:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_ts_none(self) -> None:
        self.assertIsNone(parse_ts(None))
        self.assertIsNone(parse_ts(""))
        self.assertIsNone(parse_ts("garbage"))

    def test_secs_since_none(self) -> None:
        self.assertIsNone(secs_since(None))
        self.assertIsNone(secs_since(""))

    def test_secs_since_recent(self) -> None:
        ts = utc_ts()
        s = secs_since(ts)
        self.assertIsNotNone(s)
        self.assertGreaterEqual(s, 0.0)
        self.assertLess(s, 5.0)


class TestCoerceHelpers(unittest.TestCase):
    def test_as_int_normal(self) -> None:
        self.assertEqual(as_int(42), 42)
        self.assertEqual(as_int("7"), 7)
        self.assertEqual(as_int(3.0), 3)

    def test_as_int_fallback(self) -> None:
        self.assertEqual(as_int(None, 99), 99)
        self.assertEqual(as_int(True, 99), 99)
        self.assertEqual(as_int("abc", -1), -1)

    def test_as_float_normal(self) -> None:
        self.assertAlmostEqual(as_float(3.14), 3.14)
        self.assertAlmostEqual(as_float("2.5"), 2.5)
        self.assertAlmostEqual(as_float(7), 7.0)

    def test_as_float_fallback(self) -> None:
        self.assertAlmostEqual(as_float(None, 1.0), 1.0)
        self.assertAlmostEqual(as_float(True, 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
