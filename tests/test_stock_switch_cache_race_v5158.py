import time
import unittest
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parents[1]


class StockSwitchCacheRaceTests(unittest.TestCase):
    def tearDown(self):
        server._CACHE.clear()
        server._OFFICIAL_HISTORY_CACHE.clear()

    def test_report_from_older_history_revision_is_rejected(self):
        server._OFFICIAL_HISTORY_CACHE["3665"] = (time.time(), {"revenue": [{"date": "2026-07"}]})
        old_report = {"ticker": "3665", "history_cache_at_used": 0}
        self.assertFalse(server._cached_stock_is_current("3665", old_report))

    def test_report_from_same_history_revision_is_current(self):
        revision = time.time()
        server._OFFICIAL_HISTORY_CACHE["3665"] = (revision, {"margin": [{"date": "2026-08-21"}]})
        report = {"ticker": "3665", "history_cache_at_used": revision}
        self.assertTrue(server._cached_stock_is_current("3665", report))

    def test_frontend_discards_out_of_order_stock_response(self):
        source = (ROOT / "app.js").read_text()
        self.assertIn("const requestId=++stockRequestSequence", source)
        self.assertIn("if(requestId!==stockRequestSequence)return", source)
        self.assertIn("currentTicker=ticker", source)

    def test_plain_financial_summary_does_not_render_anchor_source(self):
        source = (ROOT / "app.js").read_text()
        analysis_line = next(line for line in source.splitlines() if "$('fundAnalysis').textContent" in line)
        self.assertIn("staleText", analysis_line)
        self.assertNotIn("diagLink", analysis_line)

    def test_versioned_assets_match_runtime(self):
        html = (ROOT / "index.html").read_text()
        self.assertEqual(server.APP_VERSION, "5.17.2")
        self.assertNotIn("5.15.8", html)
        self.assertGreaterEqual(html.count(server.APP_VERSION), 4)


if __name__ == "__main__":
    unittest.main()
