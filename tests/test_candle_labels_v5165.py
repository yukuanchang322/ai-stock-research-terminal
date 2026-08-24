import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CandleLabelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "styles.css").read_text(encoding="utf-8")

    def test_chart_has_price_and_date_axes(self):
        self.assertIn('class="price-tick"', self.js)
        self.assertIn('class="candle-date"', self.js)
        self.assertIn('class="latest-price-line"', self.js)

    def test_touch_detail_is_bound_after_each_render(self):
        self.assertIn("function bindCandleTooltip(series)", self.js)
        render_at = self.js.index("$('priceChart').innerHTML=technicalDashboard(t)")
        bind_at = self.js.index("bindCandleTooltip(candleWindow(t.series||[]))", render_at)
        self.assertLess(render_at, bind_at)

    def test_tooltip_contains_ohlc_date_and_moving_averages(self):
        for label in ("開 ", "高 ", "低 ", "收 ", "漲跌 ", "MA20", "MA60"):
            self.assertIn(label, self.js)
        self.assertIn("touch-action:pan-y", self.css)

    def test_price_details_are_below_chart_not_overlaying_it(self):
        self.assertIn('class="candle-details" aria-live="polite"', self.js)
        self.assertNotIn('class="candle-tooltip"', self.js)
        self.assertNotIn("position:absolute;top:36px", self.css)


if __name__ == "__main__":
    unittest.main()
