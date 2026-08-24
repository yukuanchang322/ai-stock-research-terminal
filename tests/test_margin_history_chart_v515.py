import unittest
from pathlib import Path

import server


class MarginHistorySeriesTests(unittest.TestCase):
    def test_calc_flow_keeps_official_trading_lots_and_derives_daily_changes(self):
        rows = [
            {"date": "2026-08-19", "MarginPurchaseTodayBalance": 5_700, "ShortSaleTodayBalance": 70},
            {"date": "2026-08-20", "MarginPurchaseTodayBalance": 5_680, "ShortSaleTodayBalance": 68},
            {"date": "2026-08-21", "MarginPurchaseTodayBalance": 5_719, "ShortSaleTodayBalance": 67},
        ]

        flow = server.calc_flow([], rows)

        self.assertEqual(flow["margin_history_unit"], "trading_lots")
        self.assertEqual(len(flow["margin_history"]), 3)
        self.assertIsNone(flow["margin_history"][0]["margin_change"])
        self.assertEqual(flow["margin_history"][1]["margin_change"], -20)
        self.assertEqual(flow["margin_history"][2]["margin_change"], 39)
        self.assertEqual(flow["margin_history"][2]["short_change"], -1)

    def test_frontend_declares_two_history_charts_without_rescaling_official_lots(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn("marginHistoryCharts", html)
        self.assertIn("creditHistoryChart(series,'margin','融資')", app)
        self.assertIn("creditHistoryChart(series,'short','融券')", app)
        self.assertNotIn("Number(v)/1000", app)
        self.assertIn("官方交易單位（張）", app)


if __name__ == "__main__":
    unittest.main()
