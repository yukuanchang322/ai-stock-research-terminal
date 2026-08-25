import asyncio
import unittest
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parents[1]


class SecurityAndPerformanceV5173Tests(unittest.TestCase):
    def test_project_root_is_not_mounted_as_static(self):
        source=(ROOT/"server.py").read_text(encoding="utf-8")
        self.assertNotIn('app.mount("/static", StaticFiles(directory=ROOT)',source)
        self.assertNotIn("server.py",server.STATIC_ASSETS)
        with self.assertRaises(server.HTTPException) as ctx:
            asyncio.run(server.static_asset("server.py"))
        self.assertEqual(ctx.exception.status_code,404)

    def test_cache_clear_is_disabled_without_admin_token(self):
        self.assertEqual(server.CACHE_ADMIN_TOKEN,"")
        source=(ROOT/"server.py").read_text(encoding="utf-8")
        self.assertIn("secrets.compare_digest",source)
        self.assertIn('raise HTTPException(404,"not found")',source)

    def test_background_refresh_uses_lightweight_status_first(self):
        source=(ROOT/"app.js").read_text(encoding="utf-8")
        status_pos=source.index("/api/diagnostics/history/")
        stock_pos=source.index("/api/stock/",status_pos)
        self.assertLess(status_pos,stock_pos)
        self.assertIn("const improved=",source)

    def test_stock_payload_omits_verbose_mcp_discovery(self):
        compact=server.compact_mcp_snapshot({
            "status":"ok","records":[{"metric":"close","value":100}],"tool_count":180,
            "discovered_tools":[{"name":"huge","description":"x"*1000}],"tool_calls":[{"preview":"x"*1000}],
        })
        self.assertEqual(compact["tool_count"],180)
        self.assertNotIn("discovered_tools",compact)
        self.assertNotIn("tool_calls",compact)


if __name__ == "__main__":
    unittest.main()
