"""Smoke tests for StrategyConfig loading and validation."""

import json
import os
import tempfile
import unittest

from kalshi_edge.strategy_config import (
    StrategyConfig,
    PaperConfig,
    load_config,
    config_hash,
    config_to_dict,
    ENV_VAR,
)


class TestConfigDefaults(unittest.TestCase):
    def test_default_config_validates(self) -> None:
        cfg = StrategyConfig()
        cfg.validate()

    def test_paper_config_validates(self) -> None:
        pc = PaperConfig()
        pc.validate()

    def test_config_to_dict_roundtrip(self) -> None:
        cfg = StrategyConfig()
        d = config_to_dict(cfg)
        self.assertIsInstance(d, dict)
        self.assertIn("MIN_EV", d)
        self.assertIn("paper", d)
        self.assertIsInstance(d["paper"], dict)

    def test_config_hash_deterministic(self) -> None:
        a = StrategyConfig()
        b = StrategyConfig()
        self.assertEqual(config_hash(a), config_hash(b))


class TestConfigFromFile(unittest.TestCase):
    def test_load_example_config(self) -> None:
        example = os.path.join(os.path.dirname(__file__), "..", "strategy_config.example.json")
        if not os.path.exists(example):
            self.skipTest("strategy_config.example.json not found")
        old = os.environ.get(ENV_VAR)
        try:
            os.environ[ENV_VAR] = example
            cfg = load_config()
            self.assertIsInstance(cfg, StrategyConfig)
            self.assertGreater(cfg.MIN_EV, 0)
        finally:
            if old is None:
                os.environ.pop(ENV_VAR, None)
            else:
                os.environ[ENV_VAR] = old

    def test_load_with_overrides(self) -> None:
        data = {"MIN_EV": 0.10, "ORDER_SIZE": 3, "DEDUPE_MARKETS": False}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        old = os.environ.get(ENV_VAR)
        try:
            os.environ[ENV_VAR] = path
            cfg = load_config()
            self.assertAlmostEqual(cfg.MIN_EV, 0.10)
            self.assertEqual(cfg.ORDER_SIZE, 3)
            self.assertFalse(cfg.DEDUPE_MARKETS)
        finally:
            if old is None:
                os.environ.pop(ENV_VAR, None)
            else:
                os.environ[ENV_VAR] = old
            os.unlink(path)

    def test_invalid_json_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        old = os.environ.get(ENV_VAR)
        try:
            os.environ[ENV_VAR] = path
            with self.assertRaises(ValueError):
                load_config()
        finally:
            if old is None:
                os.environ.pop(ENV_VAR, None)
            else:
                os.environ[ENV_VAR] = old
            os.unlink(path)


class TestConfigValidation(unittest.TestCase):
    def test_negative_min_ev_raises(self) -> None:
        cfg = StrategyConfig(MIN_EV=-1.0)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_dedupe_forces_scale_in_false(self) -> None:
        cfg = StrategyConfig(DEDUPE_MARKETS=True, ALLOW_SCALE_IN=True)
        cfg.validate()
        self.assertFalse(cfg.ALLOW_SCALE_IN)


if __name__ == "__main__":
    unittest.main()
