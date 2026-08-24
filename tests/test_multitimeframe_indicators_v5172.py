import unittest

import server


class MultiTimeframeIndicatorTests(unittest.TestCase):
    def test_weekly_indicators_are_recomputed_from_weekly_ohlc(self):
        rows=[]
        start=server.date(2024,1,1)
        for i in range(420):
            day=start+server.timedelta(days=i)
            if day.weekday() < 5:
                close=100+i*.2+(8 if i%17<8 else -5)
                rows.append({"date":day.isoformat(),"open":close-1,"max":close+2,"min":close-2,"close":close,"Trading_Volume":1000+i})
        daily=server.calc_technical(rows,view_limit=None)
        weekly_rows=server.aggregate_price_history(rows,"week")
        weekly=server.calc_technical(weekly_rows,view_limit=None)
        self.assertTrue(weekly["series"])
        self.assertNotEqual(round(daily["rsi14"],4),round(weekly["rsi14"],4))
        self.assertNotEqual(round(daily["macd_hist"],4),round(weekly["macd_hist"],4))
        self.assertEqual(weekly["series"][-1]["rsi14"],weekly["rsi14"])

    def test_frontend_updates_all_indicator_surfaces(self):
        js=(server.ROOT/"app.js").read_text()
        self.assertIn("renderTechnicalSummary(cached.technical,interval)",js)
        self.assertIn("technical:data.technical||null",js)
        self.assertIn("periodIndicatorHtml(visible,candlePeriodState.interval)",js)
        self.assertIn("所有數值均由目前選取的${label} OHLC 重新計算",js)
        self.assertNotIn("下方 KD／MACD／RSI 維持日線指標",js)


if __name__ == "__main__":
    unittest.main()
