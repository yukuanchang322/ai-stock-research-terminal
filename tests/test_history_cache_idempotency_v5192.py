import unittest
from pathlib import Path
from unittest.mock import patch

import server


class HistoryCacheIdempotencyTests(unittest.TestCase):
    def setUp(self):
        server._CACHE.clear()
        server._OFFICIAL_HISTORY_CACHE.clear()

    def tearDown(self):
        server._CACHE.clear()
        server._OFFICIAL_HISTORY_CACHE.clear()

    def test_identical_history_does_not_advance_revision_or_evict_report(self):
        rows = [
            {"date": "2026-08-28", "margin_balance": 100},
            {"date": "2026-08-31", "margin_balance": 110},
        ]
        with patch.object(server.time, "time", return_value=1000):
            self.assertTrue(server._save_official_history("2454", "margin", rows))

        server._CACHE["2454"] = (1001, {"ticker": "2454", "price": 1400})
        with patch.object(server.time, "time", return_value=2000):
            self.assertFalse(server._save_official_history("2454", "margin", list(rows)))

        self.assertEqual(server._OFFICIAL_HISTORY_CACHE["2454"][0], 1000)
        self.assertIn("2454", server._CACHE)

    def test_material_history_change_advances_revision_and_evicts_report(self):
        original = [{"date": "2026-08-31", "margin_balance": 100}]
        changed = [{"date": "2026-08-31", "margin_balance": 120}]
        with patch.object(server.time, "time", return_value=1000):
            self.assertTrue(server._save_official_history("2454", "margin", original))

        server._CACHE["2454"] = (1001, {"ticker": "2454", "price": 1400})
        with patch.object(server.time, "time", return_value=2000):
            self.assertTrue(server._save_official_history("2454", "margin", changed))

        self.assertEqual(server._OFFICIAL_HISTORY_CACHE["2454"][0], 2000)
        self.assertNotIn("2454", server._CACHE)
        saved = server._OFFICIAL_HISTORY_CACHE["2454"][1]["margin"]
        self.assertEqual(saved[0]["margin_balance"], 120)

    def test_market_backfill_only_persists_when_unique_count_increases(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('unique_count > int(job.get("rows") or 0)', source)


if __name__ == "__main__":
    unittest.main()
