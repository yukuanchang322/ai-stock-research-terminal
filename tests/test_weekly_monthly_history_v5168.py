import unittest

import server


class WeeklyMonthlyHistoryV5168Tests(unittest.TestCase):
    def test_weekly_ohlc_aggregation(self):
        rows = [
            {"date": "2026-08-17", "open": 100, "max": 105, "min": 98, "close": 103, "Trading_Volume": 10},
            {"date": "2026-08-18", "open": 103, "max": 108, "min": 101, "close": 107, "Trading_Volume": 20},
            {"date": "2026-08-24", "open": 110, "max": 112, "min": 109, "close": 111, "Trading_Volume": 30},
        ]
        weekly = server.aggregate_price_history(rows, "week")
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[0]["open"], 100)
        self.assertEqual(weekly[0]["max"], 108)
        self.assertEqual(weekly[0]["min"], 98)
        self.assertEqual(weekly[0]["close"], 107)
        self.assertEqual(weekly[0]["Trading_Volume"], 30)

    def test_monthly_uses_last_trading_date(self):
        rows = [
            {"date": "2026-07-01", "open": 50, "max": 55, "min": 49, "close": 54},
            {"date": "2026-07-31", "open": 54, "max": 60, "min": 53, "close": 59},
        ]
        monthly = server.aggregate_price_history(rows, "month")
        self.assertEqual(monthly[0]["date"], "2026-07-31")
        self.assertEqual(monthly[0]["close"], 59)

    def test_frontend_has_period_switch_and_independent_route(self):
        js = (server.ROOT / "app.js").read_text()
        self.assertIn("data-candle-period=\"week\"", js)
        self.assertIn("data-candle-period=\"month\"", js)
        self.assertIn("/api/history/", js)
        self.assertIn("candlePeriodCache", js)


if __name__ == "__main__":
    unittest.main()
