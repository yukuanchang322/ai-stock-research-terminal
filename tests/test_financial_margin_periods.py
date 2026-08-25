import unittest
from unittest.mock import AsyncMock, patch

import server


class FinancialMarginPeriodTests(unittest.IsolatedAsyncioTestCase):
    async def test_q2_margins_are_derived_only_from_matching_official_ytd_amounts(self):
        current={
            "official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
            "source":"TWSE/MOPS","endpoint":"current",
            "revenue_ytd":11826622,"gross_profit_ytd":4762873,
            "operating_income_ytd":3014290,"net_income_ytd":3066460,
        }
        prior={
            "official":True,"period":"2026 Q1","fiscal_year":2026,"fiscal_quarter":1,
            "source":"Company IR","endpoint":"prior",
            "revenue_ytd":4186855,"gross_profit_ytd":2100852,
            "operating_income_ytd":1367609,"net_income_ytd":1427116,
        }
        with patch("server.fetch_official_financial_amounts_for_period",new=AsyncMock(return_value=prior)):
            views=await server.build_financial_margin_views("3661",current)
        self.assertEqual(views["ytd"]["period"],"2026 Q2 YTD")
        self.assertAlmostEqual(views["ytd"]["gross_margin"],40.2724717168)
        self.assertEqual(views["quarter"]["method"],"official_ytd_difference")
        self.assertAlmostEqual(views["quarter"]["gross_margin"],34.8442694653)
        self.assertAlmostEqual(views["quarter"]["operating_margin"],21.5540735732)
        self.assertAlmostEqual(views["quarter"]["net_margin"],21.4580366129)
        self.assertIn("非公司單季公告",views["quarter"]["note"])

    async def test_missing_predecessor_never_relabels_ytd_as_quarter(self):
        current={
            "official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
            "source":"TWSE/MOPS","revenue_ytd":100,"gross_profit_ytd":40,
            "operating_income_ytd":20,"net_income_ytd":15,
        }
        with patch("server.fetch_official_financial_amounts_for_period",new=AsyncMock(return_value=None)):
            views=await server.build_financial_margin_views("9999",current)
        self.assertIsNone(views["quarter"])
        self.assertEqual(views["ytd"]["gross_margin"],40)
        self.assertIn("不推測單季獲利率",views["warning"])

    async def test_background_fast_path_does_not_refetch_prior_quarter(self):
        current={
            "official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
            "source":"TWSE/MOPS","revenue_ytd":200,"gross_profit_ytd":80,
            "operating_income_ytd":40,"net_income_ytd":30,
        }
        resolver=AsyncMock(side_effect=AssertionError("background path must not fetch prior quarter"))
        with patch("server.fetch_official_financial_amounts_for_period",new=resolver):
            views=await server.build_financial_margin_views("2454",current,resolve_prior=False)
        resolver.assert_not_awaited()
        self.assertEqual(views["ytd"]["gross_margin"],40)
        self.assertIsNone(views["quarter"])
        self.assertIn("只顯示 2026 Q2 YTD",views["warning"])

    async def test_direct_official_quarter_margin_never_fetches_predecessor(self):
        current={
            "official":True,"period":"2026 Q2","fiscal_year":2026,"fiscal_quarter":2,
            "source":"Company IR","endpoint":"official",
            "revenue_ytd":200,"gross_profit_ytd":80,"operating_income_ytd":40,
            "net_income_ytd":30,"gross_margin_direct":46.2,"operating_margin_direct":15.2,
        }
        resolver=AsyncMock(side_effect=AssertionError("direct official margins must win"))
        with patch("server.fetch_official_financial_amounts_for_period",new=resolver):
            views=await server.build_financial_margin_views("2454",current)
        resolver.assert_not_awaited()
        self.assertEqual(views["quarter"]["method"],"company_official_direct")
        self.assertEqual(views["quarter"]["gross_margin"],46.2)
        self.assertEqual(views["quarter"]["operating_margin"],15.2)
        self.assertIsNone(views["warning"])

    def test_mops_company_parser_extracts_ytd_amount_column(self):
        html="""
        <table>
          <tr><td>營業收入</td><td>7,639,767</td><td>5,000,000</td><td>11,826,622</td><td>9,000,000</td></tr>
          <tr><td>營業毛利</td><td>2,662,021</td><td>2,000,000</td><td>4,762,873</td><td>4,000,000</td></tr>
          <tr><td>營業利益</td><td>1,646,681</td><td>1,000,000</td><td>3,014,290</td><td>2,000,000</td></tr>
          <tr><td>本期淨利</td><td>1,639,344</td><td>1,000,000</td><td>3,066,460</td><td>2,000,000</td></tr>
          <tr><td>基本每股盈餘</td><td>20.02</td><td>10.00</td><td>37.57</td><td>30.00</td></tr>
        </table>
        """
        snap=server._extract_mops_ifrs_tables(html,"3661",2026,2,"C","official")
        self.assertEqual(snap["revenue_ytd"],11826622)
        self.assertEqual(snap["gross_profit_ytd"],4762873)
        self.assertEqual(snap["operating_income_ytd"],3014290)
        self.assertEqual(snap["net_income_ytd"],3066460)
        self.assertEqual(snap["ytd_eps"],37.57)


if __name__ == "__main__":
    unittest.main()
