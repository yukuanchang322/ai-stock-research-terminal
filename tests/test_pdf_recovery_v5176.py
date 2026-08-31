import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pypdf import PdfReader

import server
from professional_pdf import _risk_levels


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
            reader=PdfReader(str(out))
            self.assertGreaterEqual(len(reader.pages),7)
            text="".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("基本面與盈餘品質",text)
            self.assertIn("法人籌碼與融資融券",text)
            self.assertIn("估值、法人預期與市場隱含假設",text)
            self.assertIn("資料來源、品質邊界與重要揭露",text)

    def test_pdf_risk_levels_never_label_above_market_as_support(self):
        technical={"support1":3815.75,"support2":4014.4,"resistance":4560,
                   "ma":{"20":3815.75,"60":4014.4},
                   "series":[{"low":3400+i} for i in range(60)]}
        support1,support2,resistance=_risk_levels(technical,3765)
        self.assertLessEqual(support2,support1)
        self.assertLessEqual(support1,3765)
        self.assertGreaterEqual(resistance,3765)

    async def test_pdf_route_uses_cache_by_default_and_inline_fallback(self):
        report=self.sample_report()
        with tempfile.TemporaryDirectory() as folder, patch.object(server,"REPORT_DIR",Path(folder)), \
             patch("server.build_stock",new=AsyncMock(return_value=report)) as build, \
             patch.object(server,"PDF_RENDERER","reportlab"), patch.object(server,"_PDF_REPORT_CACHE",{}):
            response=await server.stock_pdf("2454",refresh=False)
            build.assert_awaited_once_with("2454",force_refresh=False)
            self.assertEqual(response.media_type,"application/pdf")
            self.assertIn("inline",response.headers["content-disposition"])
            self.assertEqual(response.headers["x-pdf-renderer"],"reportlab")
            self.assertEqual(response.headers["x-pdf-cache"],"MISS")
            self.assertTrue(Path(response.path).read_bytes().startswith(b"%PDF-"))

    async def test_second_pdf_request_is_cache_hit_without_rebuilding_stock(self):
        report=self.sample_report()
        with tempfile.TemporaryDirectory() as folder, patch.object(server,"REPORT_DIR",Path(folder)), \
             patch("server.build_stock",new=AsyncMock(return_value=report)) as build, \
             patch.object(server,"PDF_RENDERER","reportlab"), patch.object(server,"_PDF_REPORT_CACHE",{}), \
             patch("server._pdf_history_revision",return_value=0):
            for ticker in ("2330","2454","3661"):
                report["ticker"]=ticker
                first=await server.stock_pdf(ticker,refresh=False)
                second=await server.stock_pdf(ticker,refresh=False)
                self.assertEqual(first.headers["x-pdf-cache"],"MISS")
                self.assertEqual(second.headers["x-pdf-cache"],"HIT")
                self.assertEqual(first.path,second.path)
            self.assertEqual(build.await_count,3)

    async def test_official_history_revision_invalidates_cached_pdf(self):
        report=self.sample_report(); revisions=iter((1,2,2,2))
        with tempfile.TemporaryDirectory() as folder, patch.object(server,"REPORT_DIR",Path(folder)), \
             patch("server.build_stock",new=AsyncMock(return_value=report)) as build, \
             patch.object(server,"PDF_RENDERER","reportlab"), patch.object(server,"_PDF_REPORT_CACHE",{}), \
             patch("server._pdf_history_revision",side_effect=lambda _ticker:next(revisions)):
            first=await server.stock_pdf("2454",refresh=False)
            second=await server.stock_pdf("2454",refresh=False)
            self.assertEqual(first.headers["x-pdf-cache"],"MISS")
            self.assertEqual(second.headers["x-pdf-cache"],"MISS")
            self.assertEqual(build.await_count,2)

    def test_pdf_history_revision_stays_stable_until_background_job_completes(self):
        with patch.object(server,"_OFFICIAL_HISTORY_CYCLES",{"2454":42}), patch("server._history_revision",return_value=99):
            first=server._pdf_history_revision("2454")
            second=server._pdf_history_revision("2454")
            self.assertEqual(first,second)
            server._OFFICIAL_HISTORY_CYCLES.pop("2454")
            self.assertEqual(server._pdf_history_revision("2454"),99)

    def test_history_cycle_clears_only_after_last_provider_finishes(self):
        class Task:
            pass
        tasks={"2454:institutional":Task(),"2454:margin":Task()}
        with patch.object(server,"_OFFICIAL_HISTORY_TASKS",tasks), patch.object(server,"_OFFICIAL_HISTORY_CYCLES",{"2454":42}):
            server._finish_official_history_task("2454","institutional")
            self.assertEqual(server._OFFICIAL_HISTORY_CYCLES["2454"],42)
            server._finish_official_history_task("2454","margin")
            self.assertNotIn("2454",server._OFFICIAL_HISTORY_CYCLES)

    def test_mobile_button_uses_cached_pdf_and_shows_busy_state(self):
        app=(server.ROOT/"app.js").read_text()
        self.assertIn("async function openPdfReport()",app)
        self.assertIn("/pdf?refresh=false",app)
        self.assertIn("PDF 產生中",app)
        self.assertIn("application/pdf",app)
        self.assertNotIn("/pdf?refresh=true",app)


if __name__ == "__main__":
    unittest.main()
