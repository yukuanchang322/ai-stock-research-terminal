import time
import unittest
from pathlib import Path

import server


class RenderHistoryBackfillV5157Tests(unittest.TestCase):
    def setUp(self):
        server._OFFICIAL_HISTORY_CACHE.clear()
        server._OFFICIAL_HISTORY_JOBS.clear()
        server._OFFICIAL_HISTORY_TASKS.clear()

    def tearDown(self):
        for task in server._OFFICIAL_HISTORY_TASKS.values():
            task.cancel()
        server._OFFICIAL_HISTORY_TASKS.clear()

    def test_partial_provider_results_are_merged_not_replaced(self):
        server._save_official_history("3661", "institutional", [{"date": "2026-08-21", "value": 1}])
        server._save_official_history("3661", "revenue", [{"date": "2026-07-01", "value": 2}])
        server._save_official_history("3661", "institutional", [{"date": "2026-08-20", "value": 3}])
        cached = server._OFFICIAL_HISTORY_CACHE["3661"][1]
        self.assertEqual(len(cached["institutional"]), 2)
        self.assertEqual(len(cached["revenue"]), 1)

    def test_scheduler_starts_three_independent_provider_jobs(self):
        # Source-level guard complements behavioral cache tests and prevents a
        # future all-in-one timeout from silently returning.
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('f"{ticker}:{provider}"', source)
        self.assertIn('requirements = {"institutional": 20, "margin": 21, "revenue": 20}', source)
        self.assertNotIn("fetch_official_market_supplements(ticker, history_days=45", source)

    def test_fresh_complete_cache_does_not_restart_jobs(self):
        server._OFFICIAL_HISTORY_CACHE["2330"] = (time.time(), {
            "institutional": [{"date": str(i)} for i in range(20)],
            "margin": [{"date": str(i)} for i in range(21)],
            "revenue": [{"date": str(i)} for i in range(20)],
        })
        server.schedule_official_history("2330")
        self.assertEqual(server._OFFICIAL_HISTORY_TASKS, {})


if __name__ == "__main__":
    unittest.main()
