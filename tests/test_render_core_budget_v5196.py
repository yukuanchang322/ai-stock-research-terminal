import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server


def otc_summary():
    return {
        "Date": "1150909", "Year": "115", "季別": "2",
        "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶",
        "基本每股盈餘": "11.87", "營業收入": "29199108",
        "營業利益": "2898184", "稅後淨利": "5674943",
    }


class RenderCoreBudgetV5196Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server._OFFICIAL_FINANCIAL_CACHE.clear()
        server._OFFICIAL_FINANCIAL_TASKS.clear()
        server._OFFICIAL_FINANCIAL_JOBS.clear()

    async def test_verified_tpex_summary_survives_slow_detail_endpoints(self):
        async def openapi(_base, path):
            if path == "/mopsfin_t187ap14_O":
                return [otc_summary()]
            await asyncio.sleep(0.2)
            return []

        with patch.object(server, "expected_latest_financial_period", return_value=(2026, 2, "2026 Q2")), \
             patch.object(server, "openapi_json", side_effect=openapi), \
             patch.object(server, "CORE_PROVIDER_TIMEOUT", 0.03):
            result = await server.fetch_official_income_statement("6488")

        self.assertTrue(result["official"])
        self.assertEqual(result["period"], "2026 Q2")
        self.assertEqual(result["ytd_eps"], 11.87)
        self.assertIn("official_detail:deferred_after_core_budget", result["errors"])

    async def test_warm_financial_caches_fast_official_snapshot_before_reconciliation(self):
        snapshot = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 11.87,
            "source": "TPEx/MOPS EPS Daily Summary",
        }
        with patch.object(server, "fetch_official_income_statement", new=AsyncMock(return_value=snapshot)), \
             patch.object(server, "reconcile_official_financial_snapshot", new=AsyncMock()) as reconcile:
            result = await server._warm_official_financial("6488")

        self.assertTrue(result["official"])
        self.assertEqual(server._OFFICIAL_FINANCIAL_CACHE["6488"][1]["ytd_eps"], 11.87)
        reconcile.assert_not_awaited()

    async def test_fast_eps_stack_skips_historical_network_resolution(self):
        official = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 11.87,
            "source": "TPEx/MOPS EPS Daily Summary",
        }
        with patch.object(server, "fetch_official_eps_for_period", new=AsyncMock()) as lookup:
            result = await server.build_eps_stack("6488", [], official, {}, resolve_history=False)

        self.assertEqual(result["ytd_eps"], 11.87)
        self.assertEqual(result["ytd_method_label"], "✅ 官方累計值")
        lookup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
