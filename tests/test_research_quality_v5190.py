import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server


class ResearchQualityTests(unittest.IsolatedAsyncioTestCase):
    def test_decision_brief_exposes_weighted_value_and_quality_gates(self):
        valuation={"scenarios":[
            {"name":"悲觀","target":80,"eps":8,"pe":10,"upside_pct":-20},
            {"name":"基準","target":100,"eps":10,"pe":10,"upside_pct":0},
            {"name":"樂觀","target":140,"eps":10,"pe":14,"upside_pct":40},
        ]}
        brief=server.build_decision_brief(100,valuation,{"target_coverage":1,"eps_coverage":0},
            {"overall":68},{"official_verified":False},{"conflicts":[]},{"rows":[]})
        self.assertEqual(brief["expected_target"],105)
        self.assertEqual(brief["expected_upside_pct"],5)
        self.assertEqual([x["probability_pct"] for x in brief["scenarios"]],[25,50,25])
        self.assertEqual(brief["conclusion_strength"],"低")
        self.assertGreaterEqual(len(brief["quality_gates"]),3)

    def test_confidence_is_capped_when_official_financial_is_unverified(self):
        sources=[{"status":"ok"} for _ in range(10)]
        result=server.calc_confidence(sources,{"confidence":100},{"target_coverage":5,"eps_coverage":5},
                                      {"official_verified":False},{"conflicts":[]})
        self.assertEqual(result["overall"],69)
        self.assertIn("official_financial_unverified",result["quality_caps"])

    def test_mobile_report_renders_decision_brief_and_scenario_probability(self):
        app=(server.ROOT/"app.js").read_text(encoding="utf-8")
        page=(server.ROOT/"index.html").read_text(encoding="utf-8")
        self.assertIn("d.decision_brief",app)
        self.assertIn("結論降級原因",app)
        self.assertIn("probability_pct",app)
        self.assertIn("研究機率",page)

    async def test_duplicate_stock_builds_share_one_inflight_task(self):
        gate=asyncio.Event()
        async def slow_build(ticker,force_refresh=False):
            await gate.wait()
            return {"ticker":ticker}
        with patch.object(server,"_build_stock_uncached",new=AsyncMock(side_effect=slow_build)), \
             patch.object(server,"_STOCK_BUILD_TASKS",{}), \
             patch.object(server,"_STOCK_BUILD_METRICS",{"started":0,"coalesced":0,"completed":0,"failed":0}):
            first=asyncio.create_task(server.build_stock("2454"))
            await asyncio.sleep(0)
            second=asyncio.create_task(server.build_stock("2454"))
            await asyncio.sleep(0)
            gate.set()
            a,b=await asyncio.gather(first,second)
            self.assertFalse(a["request_diagnostics"]["coalesced"])
            self.assertTrue(b["request_diagnostics"]["coalesced"])
            self.assertEqual(server._build_stock_uncached.await_count,1)


if __name__ == "__main__":
    unittest.main()
