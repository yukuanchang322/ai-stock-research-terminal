import unittest
import re
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]


class MobileTechnicalLayoutV5155Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css=(ROOT/"styles.css").read_text(encoding="utf-8")
        cls.html=(ROOT/"index.html").read_text(encoding="utf-8")

    def test_mobile_price_chart_is_content_sized(self):
        self.assertIn(".price-chart{height:auto;min-height:0;overflow:visible}",self.css)

    def test_nested_technical_svgs_keep_independent_heights(self):
        self.assertIn(".price-chart .candle-svg{height:250px}",self.css)
        self.assertIn(".price-chart .indicator-panel svg{height:92px}",self.css)

    def test_mobile_assets_are_cache_busted(self):
        version=re.search(r"data-app-version>V([^<]+)",self.html).group(1)
        self.assertIn(f"/static/styles.css?v={version}",self.html)
        self.assertIn(f"/static/app.js?v={version}",self.html)


if __name__ == "__main__":
    unittest.main()
