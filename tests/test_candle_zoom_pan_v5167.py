import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CandleZoomPanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "styles.css").read_text(encoding="utf-8")

    def test_accessible_zoom_controls_and_range_label_exist(self):
        for action in ('zoom-in', 'zoom-out', 'reset'):
            self.assertIn(f'data-candle-action="{action}"', self.js)
        self.assertIn('個交易日', self.js)

    def test_zoom_slices_data_and_recalculates_chart(self):
        self.assertIn('function candleWindow(rows)', self.js)
        self.assertIn('rows.slice(end-size,end)', self.js)
        self.assertIn('host.innerHTML=candleSvg(visible,candlePeriodState)', self.js)

    def test_touch_pan_and_pinch_are_supported_without_blocking_vertical_scroll(self):
        self.assertIn("gesture={kind:'pan'", self.js)
        self.assertIn("gesture={kind:'pinch'", self.js)
        self.assertIn("Math.hypot", self.js)
        self.assertIn("touch-action:pan-y", self.css)

    def test_stock_change_resets_viewport(self):
        self.assertIn("if(candlePeriodState.ticker!==d.ticker)", self.js)
        self.assertIn("ticker:d.ticker", self.js)


if __name__ == "__main__":
    unittest.main()
