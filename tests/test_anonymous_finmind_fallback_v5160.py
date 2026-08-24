import unittest
from unittest.mock import AsyncMock, patch

import server


class AnonymousFinMindFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_official_latest_row_does_not_delete_fallback_history(self):
        fallback = [{"date": "2026-08-21", "value": 1}, {"date": "2026-08-24", "value": 2}]
        official = [{"date": "2026-08-24", "value": 3, "_source": "official"}]
        merged = server.merge_market_rows_by_date(fallback, official)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["value"], 3)
        self.assertEqual(merged[-1]["_source"], "official")

    def test_finmind_market_codes_are_normalized_for_otc_routing(self):
        self.assertEqual(server.normalize_market_type("tpex"), "上櫃")
        self.assertEqual(server.normalize_market_type("twse"), "上市")

    async def test_build_uses_finmind_without_token(self):
        price_rows = [
            {"date": "2026-08-21", "stock_id": "6488", "open": 940, "max": 950, "min": 930, "close": 941, "Trading_Volume": 1000},
            {"date": "2026-08-24", "stock_id": "6488", "open": 970, "max": 1035, "min": 970, "close": 1010, "Trading_Volume": 2000},
        ]

        async def anonymous_finmind(dataset, ticker=None, start=None, end=None):
            self.assertEqual(ticker, "6488")
            if dataset == "TaiwanStockInfo":
                return [{"stock_id": "6488", "stock_name": "環球晶", "type": "上櫃", "industry_category": "半導體業"}]
            if dataset == "TaiwanStockPrice":
                return price_rows
            return []

        empty_supplements = {"institutional": [], "margin": [], "revenue": [], "valuation": []}
        with patch.object(server, "FINMIND_TOKEN", ""), \
             patch.object(server, "finmind", new=anonymous_finmind), \
             patch.object(server, "fetch_official_stock_info", new=AsyncMock(return_value=({}, ["TPEx ConnectError"]))), \
             patch.object(server, "fetch_official_stock_day", new=AsyncMock(return_value=([], "official price unavailable", ["TPEx ConnectError"]))), \
             patch.object(server, "fetch_official_market_supplements", new=AsyncMock(return_value=empty_supplements)), \
             patch.object(server, "fetch_public_research", new=AsyncMock(return_value={"rows": [], "errors": []})), \
             patch.object(server, "fetch_company_events", new=AsyncMock(return_value={"rows": [], "earnings_calls": [], "material_info": [], "errors": []})), \
             patch.object(server, "schedule_official_financial", return_value=AsyncMock(return_value={"official": False})()), \
             patch.object(server, "schedule_official_history"):
            server._CACHE.pop("6488", None)
            report = await server.build_stock("6488", force_refresh=True)

        self.assertEqual(report["name"], "環球晶")
        self.assertEqual(report["market_type"], "上櫃")
        self.assertEqual(report["price"], 1010)
        self.assertEqual(report["technical"]["last_date"], "2026-08-24")
        self.assertEqual(report["source_status"][0]["dataset"], "FinMind TaiwanStockPrice")

    async def test_health_discloses_anonymous_fallback_mode(self):
        with patch.object(server, "FINMIND_TOKEN", ""):
            payload = await server.health()
        self.assertEqual(payload["finmind_mode"], "anonymous-fallback")


if __name__ == "__main__":
    unittest.main()
