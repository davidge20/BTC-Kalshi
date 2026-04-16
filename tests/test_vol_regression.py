import unittest

import pandas as pd

from kalshi_edge.vol_regression import (
    FEATURE_COL,
    LEGACY_FEATURE_COL,
    RV_COL,
    TARGET_COL,
    VolatilityRegression,
    fixed_live_iv_proxy,
    implied_vol_proxy,
)


class TestVolRegression(unittest.TestCase):
    def test_implied_vol_proxy_matches_weighted_blend_rule(self) -> None:
        self.assertAlmostEqual(implied_vol_proxy(0.60, 1.20), 0.90, places=12)
        self.assertAlmostEqual(implied_vol_proxy(0.80, 0.40), 0.68, places=12)

    def test_fixed_live_iv_proxy_matches_requested_ratio(self) -> None:
        self.assertAlmostEqual(fixed_live_iv_proxy(0.80, 0.40), 0.70, places=12)
        self.assertAlmostEqual(fixed_live_iv_proxy(0.60, 1.20), 0.75, places=12)

    def test_fit_accepts_weighted_proxy_feature(self) -> None:
        df = pd.DataFrame(
            {
                FEATURE_COL: [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.78],
                RV_COL: [0.55, 0.57, 0.58, 0.60, 0.61, 0.63, 0.64, 0.66, 0.67, 0.69],
                TARGET_COL: [0.58, 0.60, 0.61, 0.63, 0.64, 0.66, 0.67, 0.69, 0.70, 0.72],
            }
        )
        model = VolatilityRegression().fit(df)
        self.assertTrue(model.is_fitted)
        self.assertEqual(model.feature_name, FEATURE_COL)
        self.assertGreater(model.predict(0.80, 0.70), 0.0)

    def test_fit_accepts_legacy_dvol_feature(self) -> None:
        df = pd.DataFrame(
            {
                LEGACY_FEATURE_COL: [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.78],
                RV_COL: [0.55, 0.57, 0.58, 0.60, 0.61, 0.63, 0.64, 0.66, 0.67, 0.69],
                TARGET_COL: [0.58, 0.60, 0.61, 0.63, 0.64, 0.66, 0.67, 0.69, 0.70, 0.72],
            }
        )
        model = VolatilityRegression().fit(df)
        self.assertTrue(model.is_fitted)
        self.assertEqual(model.feature_name, LEGACY_FEATURE_COL)
        self.assertGreater(model.predict(0.80, 0.70), 0.0)


if __name__ == "__main__":
    unittest.main()
