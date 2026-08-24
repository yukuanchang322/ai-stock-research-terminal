import unittest
from unittest.mock import patch

import server


class FinancialSanityTests(unittest.TestCase):
    def test_absurd_margin_is_blocked(self):
        financial={"revenue":100,"gross_profit":10755.3,"operating_income":9.9,"net_income":5}
        server.apply_financial_sanity(financial)
        self.assertIsNone(financial["gross_margin"])
        self.assertEqual(financial["operating_margin"],9.9)
        self.assertEqual(financial["margin_sanity"],"blocked_invalid_values")

    def test_calc_financials_marks_backup_and_replacement_policy(self):
        rows=[
            {"date":"2026-06-30","type":"Revenue","value":100},
            {"date":"2026-06-30","type":"GrossProfit","value":30},
        ]
        financial=server.calc_financials(rows)
        self.assertTrue(financial["provisional"])
        self.assertEqual(financial["display_badge"],"△")
        self.assertIn("自動覆蓋",financial["replacement_policy"])


class ProvisionalEpsTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_current_ytd_can_use_marked_backup_predecessor(self):
        official={"official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
                  "ytd_eps":11.87,"source":"TPEx/MOPS EPS Daily Summary","endpoint":"official"}
        finmind=[{"date":"2026-03-31","type":"BasicEarningsPerShare","value":5.0}]
        with patch("server.fetch_official_eps_for_period",return_value=None):
            stack=await server.build_eps_stack("6488",finmind,official,{})
        self.assertAlmostEqual(stack["quarter_eps"],6.87)
        self.assertEqual(stack["quarter_status"],"provisional")
        self.assertEqual(stack["quarter_badge"],"△")
        self.assertIn("自動覆蓋",stack["replacement_policy"])

    async def test_official_predecessor_always_overrides_backup(self):
        official={"official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
                  "ytd_eps":11.87,"source":"Official","endpoint":"official"}
        finmind=[{"date":"2026-03-31","type":"BasicEarningsPerShare","value":5.0}]
        async def resolver(_ticker,year,quarter):
            if (year,quarter)==(2026,1):
                return {"official":True,"period":"2026 Q1","fiscal_year":2026,"fiscal_quarter":1,
                        "ytd_eps":4.5,"quarter_eps_direct":4.5,"source":"Official Q1","endpoint":"q1"}
            return None
        with patch("server.fetch_official_eps_for_period",side_effect=resolver):
            stack=await server.build_eps_stack("6488",finmind,official,{})
        self.assertAlmostEqual(stack["quarter_eps"],7.37)
        self.assertEqual(stack["quarter_status"],"official_derived")


if __name__ == "__main__":
    unittest.main()
