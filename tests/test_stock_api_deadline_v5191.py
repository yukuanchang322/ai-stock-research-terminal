import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import server


class StockApiDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_warming_without_waiting_for_proxy(self):
        async def slow_build(*_args, **_kwargs):
            await asyncio.sleep(1)

        with patch.object(server, "build_stock", new=AsyncMock(side_effect=slow_build)), \
             patch.object(server, "STOCK_API_REQUEST_TIMEOUT", 0.01), \
             patch.object(server, "_CACHE", {}), \
             patch.object(server, "_STOCK_BUILD_TASKS", {}):
            response = await server.stock_api("2330")

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "warming")
        self.assertTrue(payload["request_diagnostics"]["background_build"])
        self.assertEqual(response.headers["retry-after"], "5")
        self.assertEqual(response.headers["x-stock-build"], "background")

    async def test_timeout_serves_last_report_as_explicit_stale_cache(self):
        cached = {"ticker": "2330", "name": "台積電", "price": 100, "cache": {"hit": False}}

        async def slow_build(*_args, **_kwargs):
            await asyncio.sleep(1)

        with patch.object(server, "build_stock", new=AsyncMock(side_effect=slow_build)), \
             patch.object(server, "STOCK_API_REQUEST_TIMEOUT", 0.01), \
             patch.object(server, "_CACHE", {"2330": (time.time(), cached)}), \
             patch.object(server, "_STOCK_BUILD_TASKS", {}):
            response = await server.stock_api("2330", refresh=True)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["ticker"], "2330")
        self.assertTrue(payload["cache"]["stale"])
        self.assertEqual(payload["request_diagnostics"]["timeout_fallback"], "stale-cache")
        self.assertEqual(response.headers["x-stock-build"], "stale-cache")
        self.assertIn("background refresh", response.headers["warning"])


if __name__ == "__main__":
    unittest.main()
