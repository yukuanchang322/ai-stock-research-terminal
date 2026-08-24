import unittest
from unittest.mock import patch

import server


class MopsHistoricalParserTests(unittest.TestCase):
    def test_parses_eps_by_header_instead_of_fixed_column(self):
        html = """
        <table>
          <tr><th>公司<br>代號</th><th>公司名稱</th><th>營業收入</th><th>基本每股盈餘（元）</th></tr>
          <tr><td>2454</td><td>聯發科</td><td>123,456</td><td>15.17</td></tr>
          <tr><td>9999</td><td>缺資料</td><td>0</td><td>--</td></tr>
        </table>
        """
        rows = server._parse_mops_historical_eps(html, 2026, 1, "sii")
        self.assertEqual(rows["2454"]["ytd_eps"], 15.17)
        self.assertEqual(rows["2454"]["quarter_eps_direct"], 15.17)
        self.assertNotIn("9999", rows)

    def test_q2_historical_value_is_cumulative_not_direct(self):
        html = """
        <table><tr><th>公司代號</th><th>公司名稱</th><th>基本每股盈餘（元）</th></tr>
        <tr><td>2454</td><td>聯發科</td><td>30.44</td></tr></table>
        """
        row = server._parse_mops_historical_eps(html, 2026, 2, "sii")["2454"]
        self.assertEqual(row["ytd_eps"], 30.44)
        self.assertIsNone(row["quarter_eps_direct"])


class EpsStackTests(unittest.IsolatedAsyncioTestCase):
    async def test_derives_q2_only_from_two_official_cumulative_values(self):
        async def resolver(_ticker, year, quarter):
            if (year, quarter) == (2026, 1):
                return {
                    "official": True, "period": "2026 Q1", "fiscal_year": 2026,
                    "fiscal_quarter": 1, "ytd_eps": 15.17, "quarter_eps_direct": 15.17,
                    "source": "MOPS historical income statement summary",
                    "endpoint": "https://mops.example/q1", "eps_provenance": "official_mops_historical",
                    "eps_confidence": 100,
                }
            return None

        official = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 30.44,
            "source": "TWSE/MOPS EPS Daily Summary", "endpoint": "/opendata/t187ap14_L",
        }
        with patch("server.fetch_official_eps_for_period", side_effect=resolver):
            stack = await server.build_eps_stack("2454", [], official, {})
        self.assertEqual(stack["quarter_eps"], 15.27)
        self.assertEqual(stack["quarter_method"], "official_ytd_difference")
        self.assertEqual(stack["quarter_method_label"], "◇ 官方累計值差額推導（非公司單季公告）")
        self.assertEqual(stack["prior_ytd_eps"], 15.17)
        self.assertEqual(stack["quarter_derivation_inputs"]["prior"]["source"], "MOPS historical income statement summary")

    async def test_marks_finmind_predecessor_as_provisional(self):
        official = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 30.44,
            "source": "TWSE/MOPS EPS Daily Summary", "endpoint": "/opendata/t187ap14_L",
        }
        finmind = [{"date": "2026-03-31", "type": "BasicEarningsPerShare", "value": 15.17}]
        with patch("server.fetch_official_eps_for_period", return_value=None):
            stack = await server.build_eps_stack("2454", finmind, official, {})
        self.assertEqual(stack["quarter_eps"], 15.27)
        self.assertEqual(stack["quarter_status"], "provisional")
        self.assertEqual(stack["quarter_badge"], "△")
        self.assertIsNone(stack["prior_ytd_eps"])

    async def test_latest_official_q1_remains_visible_when_q2_is_unavailable(self):
        official = {
            "official": True, "period": "2026 Q1", "fiscal_year": 2026,
            "fiscal_quarter": 1, "ytd_eps": 11.66, "quarter_eps_direct": 11.66,
            "source": "MOPS historical income statement summary", "endpoint": "https://mops.example/q1",
            "latest_expected_period": "2026 Q2", "current_period_status": "not_published_or_unavailable",
        }
        with patch("server.fetch_official_eps_for_period", return_value=None):
            stack = await server.build_eps_stack("3665", [], official, {})
        self.assertEqual(stack["quarter_period"], "2026 Q1")
        self.assertEqual(stack["quarter_eps"], 11.66)
        self.assertEqual(stack["quarter_method"], "official_direct")


if __name__ == "__main__":
    unittest.main()
