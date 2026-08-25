import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MobileDockVisualViewportTests(unittest.TestCase):
    def test_dock_tracks_ios_visual_viewport_and_keeps_css_fallback(self):
        app = (ROOT / "app.js").read_text()
        css = (ROOT / "styles.css").read_text()
        self.assertIn("window.visualViewport", app)
        self.assertIn("viewport.offsetTop+viewport.height-dock.offsetHeight", app)
        self.assertIn("visual-viewport-ready", app)
        self.assertIn("bottom:calc(6px + env(safe-area-inset-bottom))", css)
        self.assertIn("top:calc(var(--mobile-dock-top) - env(safe-area-inset-bottom))", css)

    def test_mobile_page_reserves_room_below_the_fixed_dock(self):
        css = (ROOT / "styles.css").read_text()
        self.assertIn("main{padding-bottom:calc(104px + env(safe-area-inset-bottom))}", css)


if __name__ == "__main__":
    unittest.main()
