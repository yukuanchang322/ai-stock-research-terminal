import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import server


ROOT = Path(__file__).resolve().parents[1]


class StaleWhileRevalidateTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        server._CACHE.clear()
        server._OFFICIAL_HISTORY_CACHE.clear()
        server._OFFICIAL_FINANCIAL_CACHE.clear()
        server._STOCK_BUILD_TASKS.clear()

    def test_financial_revision_marks_report_stale_without_deleting_it(self):
        report = {
            "ticker": "2330",
            "price": 2405,
            "history_cache_at_used": 0,
            "financial_cache_at_used": 0,
        }
        server._CACHE["2330"] = (time.time(), report)
        server._OFFICIAL_FINANCIAL_CACHE["2330"] = (
            time.time(),
            {"official": True, "period": "2026 Q2"},
        )

        self.assertFalse(server._cached_stock_is_current("2330", report))
        stale = server._stale_stock_snapshot("2330", reason="financial refresh")
        self.assertEqual(stale["ticker"], "2330")
        self.assertTrue(stale["cache"]["stale"])

    async def test_force_and_normal_retry_coalesce_by_ticker(self):
        gate = asyncio.Event()

        async def slow_build(ticker, force_refresh=False):
            await gate.wait()
            return {"ticker": ticker, "force_refresh": force_refresh}

        metrics = {"started": 0, "coalesced": 0, "completed": 0, "failed": 0}
        with patch.object(server, "_build_stock_uncached", new=AsyncMock(side_effect=slow_build)), \
             patch.object(server, "_STOCK_BUILD_TASKS", {}), \
             patch.object(server, "_STOCK_BUILD_METRICS", metrics):
            forced = asyncio.create_task(server.build_stock("2330", force_refresh=True))
            await asyncio.sleep(0)
            retry = asyncio.create_task(server.build_stock("2330", force_refresh=False))
            await asyncio.sleep(0)
            gate.set()
            first, second = await asyncio.gather(forced, retry)

        self.assertEqual(first["ticker"], "2330")
        self.assertEqual(second["ticker"], "2330")
        self.assertEqual(metrics["started"], 1)
        self.assertEqual(metrics["coalesced"], 1)

    def test_frontend_restores_last_report_and_retries_warming(self):
        source = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("loadReportSnapshot(ticker)", source)
        self.assertIn("saveReportSnapshot(d)", source)
        self.assertIn("r.status===503&&j.status==='warming'", source)
        self.assertIn("await waitForStockRetry", source)
        self.assertIn("j.cache?.stale", source)
        self.assertIn("上次成功報告會保留", source)

    def test_optional_mcp_does_not_invalidate_core_report(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        start = source.index("async def _warm_twstock_mcp")
        end = source.index("def compact_mcp_snapshot", start)
        self.assertNotIn("_CACHE.pop", source[start:end])


if __name__ == "__main__":
    unittest.main()
