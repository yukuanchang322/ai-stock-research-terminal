import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server


def official_period(ticker, year, quarter, *, ytd=None, direct=None, source="MOPS official"):
    return {
        "official": True, "company_code": str(ticker), "source": source,
        "endpoint": f"official://{ticker}/{year}Q{quarter}", "period": f"{year} Q{quarter}",
        "fiscal_year": year, "fiscal_quarter": quarter,
        "ytd_eps": ytd, "quarter_eps_direct": direct,
    }


class GenericEpsEngineTests(unittest.IsolatedAsyncioTestCase):
    def test_tpex_english_year_and_date_fields_use_canonical_parser(self):
        row = {
            "Date": "1150824", "Year": "115", "季別": "2",
            "SecuritiesCompanyCode": "6488", "基本每股盈餘": "11.87",
            "營業收入": "29199108.00", "營業利益": "2898184.00",
        }
        snapshot = server._official_row_to_snapshot(row, "TPEx/MOPS EPS Daily Summary", "/mopsfin_t187ap14_O", "上櫃")
        self.assertEqual(snapshot["fiscal_year"], 2026)
        self.assertEqual(snapshot["fiscal_quarter"], 2)
        self.assertEqual(snapshot["period"], "2026 Q2")
        self.assertEqual(snapshot["statement_date"], "2026-08-24")

    async def test_q2_official_cumulative_difference_and_ttm_for_any_ticker(self):
        ticker = "7777"
        rows = {
            (2026, 2): official_period(ticker, 2026, 2, ytd=20.0),
            (2026, 1): official_period(ticker, 2026, 1, ytd=8.0),
            (2025, 4): official_period(ticker, 2025, 4, direct=7.0),
            (2025, 3): official_period(ticker, 2025, 3, direct=6.0),
        }
        resolver = AsyncMock(side_effect=lambda _ticker, y, q: rows.get((y, q)))
        with patch.object(server, "fetch_official_eps_for_period", resolver):
            stack = await server.build_eps_stack(ticker, [], rows[(2026, 2)], {})
        self.assertEqual(stack["quarter_method"], "official_ytd_difference")
        self.assertEqual(stack["quarter_eps"], 12.0)
        self.assertEqual(stack["ttm_eps"], 33.0)
        self.assertEqual(stack["ttm_status"], "official")
        self.assertIn("非公司單季公告", stack["quarter_method_label"])

    async def test_q3_uses_prior_official_cumulative_not_q2_standalone(self):
        ticker = "8888"
        rows = {
            (2026, 3): official_period(ticker, 2026, 3, ytd=35.0),
            (2026, 2): official_period(ticker, 2026, 2, ytd=20.0),
            (2026, 1): official_period(ticker, 2026, 1, ytd=8.0),
            (2025, 4): official_period(ticker, 2025, 4, direct=7.0),
        }
        with patch.object(server, "fetch_official_eps_for_period", AsyncMock(side_effect=lambda _ticker, y, q: rows.get((y, q)))):
            stack = await server.build_eps_stack(ticker, [], rows[(2026, 3)], {})
        self.assertEqual(stack["quarter_eps"], 15.0)
        self.assertEqual(stack["ttm_eps"], 42.0)

    async def test_missing_official_predecessor_never_mixes_finmind(self):
        ticker = "9999"
        current = official_period(ticker, 2026, 2, ytd=20.0)
        finmind = [{"date": "2026-03-31", "type": "BasicEarningsPerShare", "value": 8.0}]
        with patch.object(server, "fetch_official_eps_for_period", AsyncMock(return_value=None)):
            stack = await server.build_eps_stack(ticker, finmind, current, {})
        self.assertIsNone(stack["quarter_eps"])
        self.assertIsNone(stack["ttm_eps"])
        self.assertNotIn("hybrid", str(stack))
        valuation = server.model_valuation(100, {"per": 10}, stack, {}, {"core_financials_allowed": True})
        self.assertEqual(valuation["selected_model"], "annualized_ytd_estimate")
        self.assertEqual(valuation["anchor_eps"], 40.0)
        self.assertIn("年化暫估", valuation["eps_basis"])

    async def test_company_official_direct_quarter_wins(self):
        ticker = "7777"
        current = official_period(ticker, 2026, 2, ytd=20.0, direct=13.0)
        with patch.object(server, "fetch_official_eps_for_period", AsyncMock(return_value=None)):
            stack = await server.build_eps_stack(ticker, [], current, {})
        self.assertEqual(stack["quarter_eps"], 13.0)
        self.assertEqual(stack["quarter_method"], "official_direct")

    async def test_wrong_ticker_official_payload_is_rejected(self):
        wrong = official_period("6665", 2026, 2, ytd=99.0)
        stack = await server.build_eps_stack("3665", [], wrong, {})
        self.assertFalse(stack["identity_verified"])
        self.assertEqual(stack["rejected_company_code"], "6665")
        self.assertIsNone(stack["quarter_eps"])
        self.assertIsNone(stack["ytd_eps"])

    async def test_concurrent_tickers_keep_evidence_isolated(self):
        currents = {
            "7777": official_period("7777", 2026, 2, ytd=20.0),
            "8888": official_period("8888", 2026, 2, ytd=50.0),
        }
        q1 = {"7777": 8.0, "8888": 30.0}
        async def resolver(ticker, year, quarter):
            if (year, quarter) == (2026, 1):
                return official_period(ticker, year, quarter, ytd=q1[ticker])
            return None
        with patch.object(server, "fetch_official_eps_for_period", side_effect=resolver):
            left, right = await asyncio.gather(
                server.build_eps_stack("7777", [], currents["7777"], {}),
                server.build_eps_stack("8888", [], currents["8888"], {}),
            )
        self.assertEqual(left["quarter_eps"], 12.0)
        self.assertEqual(right["quarter_eps"], 20.0)
        self.assertNotEqual(left["quarter_source_url"], right["quarter_source_url"])


if __name__ == "__main__":
    unittest.main()
