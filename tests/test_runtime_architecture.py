import unittest
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parents[1]


class RuntimeArchitectureTests(unittest.TestCase):
    def test_render_uses_only_canonical_server_app(self):
        render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("uvicorn server:app", render)
        self.assertNotIn("run_v", render)

    def test_public_routes_have_one_owner(self):
        public = ("/", "/health", "/api/stock/{ticker}")
        for path in public:
            routes = [route for route in server.app.routes if getattr(route, "path", None) == path]
            self.assertEqual(len(routes), 1, path)
            self.assertEqual(routes[0].endpoint.__module__, "server")

    def test_shell_has_one_version_and_one_script(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"V{server.APP_VERSION}", html)
        self.assertEqual(html.count("app.js?v="), 1)
        self.assertNotIn("recovery.js", html)
        self.assertNotIn("_hotfix.js", html)
        self.assertNotIn("serviceWorker.register", html + js)

    def test_t86_parser_keeps_foreign_field_with_exclusion_phrase(self):
        row = {
            "證券代號": "2330",
            "外陸資買進股數(不含外資自營商)": "14,806,589",
            "外陸資賣出股數(不含外資自營商)": "9,011,543",
            "外陸資買賣超股數(不含外資自營商)": "5,795,046",
        }
        self.assertEqual(server._t86_participant(row, "foreign")["net"], 5_795_046)

    def test_t86_dealer_does_not_match_foreign_self_dealer_phrase(self):
        row = {
            "證券代號": "2330",
            "外陸資買賣超股數(不含外資自營商)": "5,795,046",
            "自營商買賣超股數": "456,923",
            "自營商買進股數(自行買賣)": "488,720",
            "自營商賣出股數(自行買賣)": "81,357",
            "自營商買進股數(避險)": "251,419",
            "自營商賣出股數(避險)": "201,859",
        }
        self.assertEqual(server._t86_participant(row, "dealer")["net"], 456_923)


if __name__ == "__main__":
    unittest.main()
