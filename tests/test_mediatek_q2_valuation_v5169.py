import unittest

import server


class MediaTekQ2ValuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_q2_difference_and_ttm(self):
        official = {
            "official": True, "source": "TWSE/MOPS EPS Daily Summary",
            "endpoint": "/opendata/t187ap14_L", "fiscal_year": 2026,
            "fiscal_quarter": 2, "period": "2026 Q2", "ytd_eps": 30.44,
        }
        stack = await server.build_eps_stack("2454", [], official, {})
        self.assertAlmostEqual(stack["quarter_eps"], 15.27, places=2)
        self.assertEqual(stack["quarter_method"], "official_ytd_difference")
        self.assertIn("非公司單季公告", stack["quarter_method_label"])
        self.assertAlmostEqual(stack["ttm_eps"], 60.67, places=2)
        self.assertEqual(len(stack["history"]), 4)

    def test_registry_uses_only_official_mediatek_sources(self):
        expected = {(2025, 3): 15.84, (2025, 4): 14.39, (2026, 1): 15.17}
        for period, eps in expected.items():
            row = server.registry_eps_for_period("2454", *period)
            self.assertTrue(row["official"])
            self.assertAlmostEqual(row["quarter_eps_direct"], eps, places=2)
            self.assertIn("mediatek.com", row["endpoint"])
        q2 = server.registry_eps_for_period("2454", 2026, 2)
        self.assertIsNone(q2["quarter_eps_direct"])
        self.assertAlmostEqual(q2["ytd_eps"], 30.44, places=2)

    def test_ttm_is_selected_instead_of_half_year_ytd(self):
        valuation = server.model_valuation(
            1400, {"per": 23.1, "pbr": 5.0, "pe_p25": 16, "pe_median": 20, "pe_p75": 24},
            {"ttm_eps": 60.67, "ytd_eps": 30.44, "quarter_period": "2026 Q2"}, {},
            {"core_financials_allowed": True},
        )
        self.assertEqual(valuation["selected_model"], "trailing_ttm")
        self.assertAlmostEqual(valuation["anchor_eps"], 60.67, places=2)
        self.assertNotAlmostEqual(valuation["anchor_eps"], 30.44, places=2)
        self.assertAlmostEqual(valuation["market_implied"]["implied_pe"], 1400 / 60.67, places=3)

    def test_partial_ytd_is_annualized_and_explicitly_provisional(self):
        valuation = server.model_valuation(
            1400, {"per": 23.1}, {"ytd_eps": 30.44, "quarter_period": "2026 Q2"}, {},
            {"core_financials_allowed": True},
        )
        self.assertEqual(valuation["selected_model"], "annualized_ytd_estimate")
        self.assertAlmostEqual(valuation["anchor_eps"], 60.88, places=2)
        self.assertIn("年化暫估", valuation["eps_basis"])

    def test_per_rows_are_filtered_to_requested_ticker(self):
        rows = [
            {"stock_id": "6665", "date": "2026-08-24", "PER": 62.2, "PBR": 14.2},
            {"stock_id": "2454", "date": "2026-08-24", "PER": 23.1, "PBR": 5.0},
        ]
        result = server.calc_per(rows, "2454")
        self.assertEqual(result["per"], 23.1)
        self.assertEqual(result["pbr"], 5.0)


if __name__ == "__main__":
    unittest.main()
