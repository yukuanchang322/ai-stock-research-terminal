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
        self.assertIn('class="latest-price-label"', self.js)

    def test_touch_detail_is_bound_after_each_render(self):
        self.assertIn("function bindCandleTooltip(series)", self.js)
        render_at = self.js.index("$('priceChart').innerHTML=technicalDashboard(t)")
        bind_at = self.js.index("bindCandleTooltip(t.series||[])", render_at)
        self.assertLess(render_at, bind_at)

    def test_tooltip_contains_ohlc_date_and_moving_averages(self):
        for label in ("開 ", "高 ", "低 ", "收 ", "漲跌 ", "MA20", "MA60"):
            self.assertIn(label, self.js)
        self.assertIn("touch-action:pan-y", self.css)


if __name__ == "__main__":
    unittest.main()
