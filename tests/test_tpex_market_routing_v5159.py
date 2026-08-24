import unittest
from unittest.mock import AsyncMock, patch

import server


class TpexMarketRoutingV5159Tests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_market_falls_back_from_twse_to_tpex(self):
        otc_rows = [{"date": "2026-08-24", "close": 1010.0}]
        with patch("server.fetch_twse_stock_day", new=AsyncMock(return_value=([], ["no rows"]))) as twse, \
             patch("server.fetch_tpex_stock_day", new=AsyncMock(return_value=(otc_rows, []))) as tpex:
            rows, provider, errors = await server.fetch_official_stock_day("6488")
        self.assertEqual(rows, otc_rows)
        self.assertEqual(provider, "TPEx afterTrading/tradingStock")
        self.assertIn("no rows", errors)
        twse.assert_awaited_once_with("6488", 13)
        tpex.assert_awaited_once_with("6488", 13)

    async def test_tpex_company_info_is_used_after_twse_miss(self):
        otc = {"stock_id": "6488", "stock_name": "環球晶", "type": "上櫃"}
        with patch("server.fetch_twse_stock_info", new=AsyncMock(return_value=({}, ["no match"]))), \
             patch("server.fetch_tpex_stock_info", new=AsyncMock(return_value=(otc, []))):
            info, errors = await server.fetch_official_stock_info("6488")
        self.assertEqual(info, otc)
        self.assertEqual(errors, [])

    def test_tpex_parser_preserves_ticker_and_source_for_runtime(self):
        payload = {"stat": "ok", "tables": [{"data": [["115/08/24", "11,429", "11,575,643", "970", "1,035", "970", "1,010", "+69", "8,368"]]}]}
        row = server.parse_tpex_stock_day_payload(payload, "6488")[0]
        self.assertEqual(row["stock_id"], "6488")
        self.assertEqual(row["Trading_Volume"], 11_429_000)
        self.assertEqual(row["close"], 1010)
        self.assertEqual(row["_source"], "TPEx afterTrading/tradingStock")


if __name__ == "__main__":
    unittest.main()
