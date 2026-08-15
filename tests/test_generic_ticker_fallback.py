import unittest
from unittest.mock import AsyncMock, patch

import server


class TpexPriceParserTests(unittest.TestCase):
    def test_normalizes_tpex_monthly_ohlc_and_converts_lots_to_shares(self):
        payload = {
            "stat": "ok",
            "tables": [{
                "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤", "漲跌", "筆數"],
                "data": [["115/08/14", "859", "2,554,994", "2,830.00", "3,100.00", "2,810.00", "2,990.00", "170.00", "3,536"]],
            }],
        }
        rows = server.parse_tpex_stock_day_payload(payload)
        self.assertEqual(rows, [{
            "date": "2026-08-14", "open": 2830.0, "max": 3100.0,
            "min": 2810.0, "close": 2990.0, "Trading_Volume": 859000.0,
        }])

    def test_rejects_non_ok_payload(self):
        with self.assertRaises(RuntimeError):
            server.parse_tpex_stock_day_payload({"stat": "參數輸入錯誤"})


class MissingValueSemanticsTests(unittest.TestCase):
    def test_unknown_institutional_columns_are_missing_not_zero(self):
        rows = [{"date": "2026-08-14", "stock_id": "6510", "unexpected": "123"}]
        result = server.calc_flow(rows, [])
        self.assertIsNone(result["foreign_20"])
        self.assertIsNone(result["trust_20"])
        self.assertIsNone(result["dealer_20"])
        self.assertNotIn("last_date", result)

    def test_known_zero_net_flow_remains_real_zero(self):
        rows = [{
            "date": "2026-08-14", "stock_id": "6510",
            "Foreign_Investor_buy": "1000", "Foreign_Investor_sell": "1000",
        }]
        result = server.calc_flow(rows, [])
        self.assertEqual(result["foreign_1"], 0.0)
        self.assertEqual(result["last_date"], "2026-08-14")
        self.assertIsNone(result["trust_1"])


class OfficialMarketRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_otc_market_uses_tpex_price_first(self):
        otc_rows = [{"date": "2026-08-14", "open": 1.0, "max": 2.0, "min": 1.0, "close": 2.0, "Trading_Volume": 1000.0}]
        with patch("server.fetch_tpex_stock_day", new=AsyncMock(return_value=(otc_rows, []))) as tpex, \
             patch("server.fetch_twse_stock_day", new=AsyncMock(return_value=([], []))) as twse:
            rows, provider, errors = await server.fetch_official_stock_day("6510", "上櫃")
        self.assertEqual(rows, otc_rows)
        self.assertEqual(provider, "TPEx afterTrading/tradingStock")
        self.assertEqual(errors, [])
        tpex.assert_awaited_once_with("6510", 13)
        twse.assert_not_awaited()

    async def test_official_info_accepts_otc_when_listed_feed_has_no_match(self):
        otc_info = {"stock_id": "6510", "stock_name": "精測", "industry_category": "產業代碼 24", "type": "上櫃"}
        with patch("server.fetch_twse_stock_info", new=AsyncMock(return_value=({}, []))), \
             patch("server.fetch_tpex_stock_info", new=AsyncMock(return_value=(otc_info, []))):
            info, errors = await server.fetch_official_stock_info("6510")
        self.assertEqual(info["stock_name"], "精測")
        self.assertEqual(info["type"], "上櫃")
        self.assertEqual(errors, [])

    async def test_reconciliation_promotes_expected_official_quarter(self):
        stale = {
            "official": True, "period": "2026 Q1", "fiscal_year": 2026,
            "fiscal_quarter": 1, "ytd_eps": 10.43, "source": "older official feed",
            "completeness": 1,
        }
        current = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 25.45,
            "source": "MOPS historical income statement summary", "completeness": 1,
        }
        with patch("server.fetch_mops_csv_official", new=AsyncMock(return_value=[])), \
             patch("server.fetch_official_eps_for_period", new=AsyncMock(return_value=current)):
            result = await server.reconcile_official_financial_snapshot("6510", stale)
        self.assertEqual(result["period"], "2026 Q2")
        self.assertEqual(result["ytd_eps"], 25.45)


if __name__ == "__main__":
    unittest.main()
