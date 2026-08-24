import unittest

import server


class ResearchIdentityTests(unittest.TestCase):
    def test_company_identity_rejects_unrelated_search_result(self):
        self.assertTrue(server._research_mentions_company("聯發科 2454 目標價上調", "2454", "聯發科"))
        self.assertFalse(server._research_mentions_company("IC測試大廠目標價上看7000元", "2454", "聯發科"))

    def test_target_parser_supports_commas_without_reading_ticker(self):
        self.assertEqual(server._extract_target("高盛挺聯發科 喊6,800元"), 6800)
        self.assertIsNone(server._extract_target("聯發科股票代號2454"))

    def test_eps_parser_rejects_target_table_number(self):
        self.assertIsNone(server._extract_eps("2027年大爆發EPS、目標價1表看"))
        self.assertEqual(server._extract_eps("2027年 EPS 可達132.18元"), 132.18)


class ValuationLayerTests(unittest.TestCase):
    def test_large_market_re_rating_is_labeled_and_penalized(self):
        research={"median_target":5000,"target_coverage":2,"eps_coverage":0}
        result=server.model_valuation(3765,{"per":62.18,"pe_p25":19.73,"pe_median":21.2,"pe_p75":23.57,"sample_count":731},
            {"ttm_eps":60.67,"quarter_period":"2026 Q2"},research,{"core_financials_allowed":True})
        self.assertEqual(result["selected_model"],"trailing_ttm")
        self.assertTrue(result["regime_shift_warning"])
        self.assertIn("不可直接視為短期目標價",result["valuation_warning"])
        self.assertEqual(result["analyst_consensus"]["status"],"consensus")
        self.assertLess(result["confidence"],70)

    def test_two_forward_estimates_take_priority_but_stay_separate(self):
        research={"median_forward_eps":132.18,"forward_eps_year":2027,"eps_coverage":2,"median_target":5000,"target_coverage":2}
        result=server.model_valuation(3765,{"pe_p25":19.73,"pe_median":21.2,"pe_p75":23.57,"sample_count":731},
            {"ttm_eps":60.67,"quarter_period":"2026 Q2"},research,{"core_financials_allowed":True})
        self.assertEqual(result["selected_model"],"forward_consensus")
        self.assertAlmostEqual(result["trailing_scenarios"][1]["target"],60.67*21.2)
        self.assertAlmostEqual(result["forward_scenarios"][1]["target"],132.18*21.2)
        self.assertEqual(result["analyst_consensus"]["median_target"],5000)

    def test_single_forward_estimate_is_visible_but_not_selected_as_consensus(self):
        research={"median_forward_eps":132.18,"forward_eps_year":2027,"eps_coverage":1,"median_target":5000,"target_coverage":1}
        result=server.model_valuation(3765,{"pe_p25":19.73,"pe_median":21.2,"pe_p75":23.57,"sample_count":731},
            {"ttm_eps":60.67,"quarter_period":"2026 Q2"},research,{"core_financials_allowed":True})
        self.assertEqual(result["selected_model"],"trailing_ttm")
        self.assertEqual(result["forward_status"],"single_source")
        self.assertTrue(result["forward_scenarios"])


if __name__ == "__main__":
    unittest.main()
