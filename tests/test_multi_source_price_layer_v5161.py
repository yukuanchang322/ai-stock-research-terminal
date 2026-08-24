import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import server


class MultiSourcePriceLayerTests(unittest.IsolatedAsyncioTestCase):
    def test_yahoo_chart_normalizes_ohlcv(self):
        payload = {"chart": {"result": [{"timestamp": [1787529600], "indicators": {"quote": [{
            "open": [970], "high": [1035], "low": [970], "close": [1010], "volume": [11898166]
        }]}}]}}
        rows = server.parse_yahoo_chart_payload(payload, "6488")
        self.assertEqual(rows[0]["stock_id"], "6488")
        self.assertEqual(rows[0]["close"], 1010)
        self.assertEqual(rows[0]["Trading_Volume"], 11898166)
        self.assertEqual(rows[0]["_source"], "Yahoo Finance chart API")

    async def test_otc_market_tries_two_suffix_first(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"chart": {"result": [{"timestamp": [1787529600], "indicators": {"quote": [{
            "open": [970], "high": [1035], "low": [970], "close": [1010], "volume": [100]
        }]}}]}}
        client = AsyncMock()
        client.get.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("server.httpx.AsyncClient", return_value=context):
            rows, provider, errors = await server.fetch_yahoo_stock_history("6488", "上櫃")
        self.assertEqual(len(rows), 1)
        self.assertEqual(provider, "Yahoo Finance 6488.TWO")
        self.assertEqual(errors, [])
        self.assertIn("6488.TWO", client.get.await_args.args[0])

    def test_official_same_day_price_wins_without_deleting_history(self):
        fallback = [{"date": "2026-08-21", "close": 941}, {"date": "2026-08-24", "close": 1000}]
        official = [{"date": "2026-08-24", "close": 1010, "_source": "TPEx"}]
        merged = server.merge_market_rows_by_date(fallback, official)
        self.assertEqual([row["close"] for row in merged], [941, 1010])


if __name__ == "__main__":
    unittest.main()
