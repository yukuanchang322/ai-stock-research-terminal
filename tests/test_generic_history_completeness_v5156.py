import asyncio
import unittest
from pathlib import Path

import server


ROOT=Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self,content):
        self.content=content

    def raise_for_status(self):
        return None


class FakeMopsClient:
    def __init__(self):
        self.urls=[]

    async def get(self,url):
        self.urls.append(url)
        if url.endswith("_1.html") and "/sii/" in url:
            return FakeResponse(b"<table><tr><td>3661</td><td>Alchip</td><td>123,456</td></tr></table>")
        return FakeResponse(b"<table></table>")


class GenericHistoryCompletenessV5156Tests(unittest.TestCase):
    def test_mops_partition_one_is_used_for_ky_issuer(self):
        async def exercise():
            client=FakeMopsClient()
            rows=await server.fetch_mops_monthly_revenue_history(
                client,"3661",server.date(2026,8,24),months=1
            )
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["revenue"],123456000)
            self.assertTrue(any(url.endswith("_1.html") for url in client.urls))
        asyncio.run(exercise())

    def test_client_rechecks_background_history_promptly(self):
        js=(ROOT/"app.js").read_text(encoding="utf-8")
        self.assertIn("institutionalLength>=20",js)
        self.assertIn("},8000);",js)
        self.assertNotIn("},55000);",js)

    def test_source_status_distinguishes_warming_from_complete(self):
        source=(ROOT/"server.py").read_text(encoding="utf-8")
        self.assertIn('"warming" if flow.get("last_date") else "missing"',source)
        self.assertIn('len(flow.get("margin_history") or [])>=21',source)


if __name__ == "__main__":
    unittest.main()
