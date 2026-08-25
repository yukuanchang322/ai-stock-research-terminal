import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pypdf import PdfReader

import server


class PdfRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def sample_report(self):
        return {
            "ticker":"2454","name":"聯發科","generated_at":"2026-08-25T12:00:00+08:00","price":3765,
            "thesis":"官方財報與市場價格交叉檢查。","scores":{"綜合":80},"financial":{"period":"2026 Q2","ttm_eps":60.67},
            "revenue":{"latest_revenue":48474930000,"revenue_yoy":12.2},"technical":{"trend":"整理"},
            "confidence":{"overall":92},"valuation":{"scenarios":[{"name":"基準","eps":60.67,"pe":25,"target":1517,"upside_pct":-59.7}]},
            "catalysts":["營收成長"],"risks":["估值偏高"],
        }

    def test_reportlab_fallback_produces_valid_pdf(self):
        self.assertTrue((server.ROOT/"assets"/"fonts"/"NotoSansTC-VF.ttf").is_file())
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/"fallback.pdf"
            server.write_reportlab_fallback_pdf(self.sample_report(),out)
            self.assertTrue(out.read_bytes().startswith(b"%PDF-"))
            self.assertGreaterEqual(len(PdfReader(str(out)).pages),1)

    async def test_pdf_route_uses_cache_by_default_and_inline_fallback(self):
        report=self.sample_report()
        class BrokenHTML:
            def __init__(self,**_kwargs):pass
            def write_pdf(self,_out):raise RuntimeError("renderer failed")
        fake_weasyprint=types.SimpleNamespace(HTML=BrokenHTML)
        with tempfile.TemporaryDirectory() as folder, patch.object(server,"REPORT_DIR",Path(folder)), \
             patch("server.build_stock",new=AsyncMock(return_value=report)) as build, \
             patch.dict(sys.modules,{"weasyprint":fake_weasyprint}):
            response=await server.stock_pdf("2454",refresh=False)
            build.assert_awaited_once_with("2454",force_refresh=False)
            self.assertEqual(response.media_type,"application/pdf")
            self.assertIn("inline",response.headers["content-disposition"])
            self.assertEqual(response.headers["x-pdf-renderer"],"reportlab-fallback")
            self.assertTrue(Path(response.path).read_bytes().startswith(b"%PDF-"))

    def test_mobile_button_uses_cached_pdf_and_shows_busy_state(self):
        app=(server.ROOT/"app.js").read_text()
        self.assertIn("async function openPdfReport()",app)
        self.assertIn("/pdf?refresh=false",app)
        self.assertIn("PDF 產生中",app)
        self.assertIn("application/pdf",app)
        self.assertNotIn("/pdf?refresh=true",app)


if __name__ == "__main__":
    unittest.main()
