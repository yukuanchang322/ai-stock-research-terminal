import unittest
from unittest.mock import AsyncMock, patch

import server


def tpex_summary(ticker="6488", eps="11.87"):
    return {
        "Date": "1150908", "Year": "115", "季別": "2",
        "SecuritiesCompanyCode": ticker, "CompanyName": "環球晶",
        "基本每股盈餘": eps, "營業收入": "29199108.00",
        "營業利益": "2898184.00", "稅後淨利": "5674943.00",
    }


def tpex_detail(ticker="6488", eps="11.87"):
    return {
        "Date": "1150908", "Year": "115", "Season": "2",
        "SecuritiesCompanyCode": ticker, "CompanyName": "環球晶",
        "基本每股盈餘（元）": eps, "營業收入": "29199108.00",
        "營業毛利（毛損）": "6053939.00", "營業利益（損失）": "2898184.00",
        "本期淨利（淨損）": "5674943.00",
    }


class FastOfficialFinancialV5195Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server._OFFICIAL_EPS_SNAPSHOT_CACHE.clear()

    async def test_tpex_ci_fast_path_returns_current_official_without_slow_fallbacks(self):
        async def openapi(_base, path):
            if path == "/mopsfin_t187ap14_O":
                return [tpex_summary()]
            if path == "/mopsfin_t187ap06_O_ci":
                return [tpex_detail()]
            return []

        slow_names = (
            "fetch_mops_company_ifrs", "fetch_mops_csv_official",
            "fetch_mops_material_financial", "fetch_company_ir_financial",
        )
        slow_mocks = {name: AsyncMock(return_value=[]) for name in slow_names}
        slow_mocks["fetch_company_ir_financial"] = AsyncMock(return_value=None)
        with patch.object(server, "expected_latest_financial_period", return_value=(2026, 2, "2026 Q2")), \
             patch.object(server, "openapi_json", side_effect=openapi) as api, \
             patch.multiple(server, **slow_mocks):
            result = await server.fetch_official_income_statement("6488")

        self.assertTrue(result["official"])
        self.assertEqual(result["period"], "2026 Q2")
        self.assertEqual(result["market"], "上櫃")
        self.assertEqual(result["ytd_eps"], 11.87)
        self.assertEqual(result["revenue_ytd"], 29199108.0)
        self.assertIn("/mopsfin_t187ap06_O_ci", [call.args[1] for call in api.await_args_list])
        for name, mock in slow_mocks.items():
            with self.subTest(slow_source=name):
                mock.assert_not_awaited()

    async def test_tpex_summary_routes_remaining_probes_only_to_otc(self):
        async def openapi(_base, path):
            if path == "/mopsfin_t187ap14_O":
                return [tpex_summary("6223", "28.08")]
            if path == "/mopsfin_t187ap06_O_mim":
                return [tpex_detail("6223", "28.08")]
            return []

        with patch.object(server, "expected_latest_financial_period", return_value=(2026, 2, "2026 Q2")), \
             patch.object(server, "openapi_json", side_effect=openapi) as api:
            result = await server.fetch_official_income_statement("6223")

        probed = [call.args[1] for call in api.await_args_list]
        self.assertTrue(result["official"])
        self.assertEqual(result["ytd_eps"], 28.08)
        self.assertIn("/mopsfin_t187ap06_O_mim", probed)
        self.assertNotIn("/opendata/t187ap06_L_mim", probed)
        self.assertNotIn("/opendata/t187ap06_X_mim", probed)

    async def test_verified_snapshot_is_reused_by_exact_period_eps_lookup(self):
        cached = server._official_row_to_snapshot(
            tpex_detail(), "TPEx/MOPS Income Statement", "/mopsfin_t187ap06_O_ci", "上櫃", "detail"
        )
        server._OFFICIAL_EPS_SNAPSHOT_CACHE[("6488", 2026, 2)] = cached
        with patch.object(server, "fetch_mops_csv_official", new=AsyncMock()) as csv_source:
            result = await server.fetch_official_eps_for_period("6488", 2026, 2)
        self.assertEqual(result["ytd_eps"], 11.87)
        self.assertEqual(result["eps_provenance"], "official_snapshot_cache")
        csv_source.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
