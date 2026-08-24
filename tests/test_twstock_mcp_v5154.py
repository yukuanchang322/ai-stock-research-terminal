import asyncio
import json
import unittest

import httpx

import server


class TwstockMcpV5154Tests(unittest.TestCase):
    def test_decodes_streamable_http_sse(self):
        payload={"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"quote"}]}}
        response=httpx.Response(
            200,
            headers={"content-type":"text/event-stream"},
            text=f"event: message\ndata: {json.dumps(payload)}\n\n",
        )
        self.assertEqual(server._mcp_response_json(response),payload)

    def test_optional_source_does_not_lower_boundary_grade(self):
        boundary=server.build_data_boundary(
            [{"name":"TWStock MCP 二次驗證（選用）","status":"optional","required":False}],
            {"official_verified":True},
            {"conflicts":[]},
        )
        self.assertEqual(boundary["grade"],"A")
        self.assertEqual(boundary["optional_unavailable_count"],1)

    def test_tool_arguments_keep_ticker_out_of_date_fields(self):
        args=server._args_for_mcp_tool({"inputSchema":{"properties":{
            "date":{"type":"string"},"stock_no":{"type":"string"},
            "stock_nos":{"type":"array","items":{"type":"string"}},
        },"required":["date"]}},"2330")
        self.assertEqual(args["stock_no"],"2330")
        self.assertEqual(args["stock_nos"],["2330"])
        self.assertRegex(args["date"],r"^20\d{6}$")

    def test_extracts_metrics_from_text_tool_results(self):
        quote="2330 台積電 | 成交: 2375.0000 | 20260824 13:30:00"
        value,path=server._extract_mcp_metric(quote,["成交"])
        self.assertEqual(value,2375.0)
        self.assertEqual(path,"text:成交")
        self.assertEqual(server._extract_mcp_date(quote),"2026-08-24")

    def test_required_missing_source_still_lowers_grade(self):
        boundary=server.build_data_boundary(
            [{"name":"財務報表","status":"missing"}],
            {"official_verified":True},
            {"conflicts":[]},
        )
        self.assertEqual(boundary["grade"],"B")
        self.assertIn("財務報表",boundary["message"])

    def test_cache_returns_pending_without_blocking(self):
        async def exercise():
            original=server._warm_twstock_mcp
            async def no_op(_ticker):
                return None
            server._warm_twstock_mcp=no_op
            try:
                snapshot=server.get_twstock_mcp_snapshot_cached("ZZTEST")
                self.assertEqual(snapshot["status"],"pending")
                self.assertTrue(snapshot["optional"])
                await asyncio.sleep(0)
            finally:
                server._warm_twstock_mcp=original
                server._MCP_TASKS.pop("ZZTEST",None)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
