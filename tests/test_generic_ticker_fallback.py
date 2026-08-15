import unittest
from unittest.mock import AsyncMock, patch

import server


class CompanyEventIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_another_ticker_and_accepts_target_company(self):
        self.assertEqual(server._company_event_identity("6488","環球晶","台積電 2330 法說會"), (False,"different_ticker_in_title"))
        self.assertEqual(server._company_event_identity("6488","環球晶","環球晶法說：展望新產能"), (True,"company_name_in_title"))
        self.assertEqual(server._company_event_identity("6488","環球晶","6488 法人說明會重點"), (True,"ticker_in_title"))

    async def test_company_events_never_include_cross_ticker_news(self):
        news=[
            {"title":"台積電 2330 法說會釋出展望","url":"https://example.com/wrong","snippet":"環球晶供應鏈相關","published_date":"2026-08-15","publisher":"Example"},
            {"title":"環球晶法說會：產能展望","url":"https://example.com/right","snippet":"公司說明營運展望","published_date":"2026-08-14","publisher":"Example"},
        ]
        with patch.object(server,"google_news_rss",new=AsyncMock(return_value=news)), patch.object(server,"fetch_official_material_info",new=AsyncMock(return_value=([],[]))):
            result=await server.fetch_company_events("6488","環球晶")
        self.assertTrue(result["rows"])
        self.assertTrue(all(row["ticker"]=="6488" and row["identity_verified"] for row in result["rows"]))
        self.assertNotIn("台積電", " ".join(row["title"] for row in result["rows"]))


class OfficialSupplementalFallbackTests(unittest.TestCase):
    def test_parses_twse_institutional_and_margin_for_exact_ticker(self):
        institutional={"fields":["證券代號","證券名稱","外陸資買進股數(不含外資自營商)","外陸資賣出股數(不含外資自營商)","外資自營商買進股數","外資自營商賣出股數","投信買進股數","投信賣出股數","自營商買進股數(自行買賣)","自營商賣出股數(自行買賣)","自營商買進股數(避險)","自營商賣出股數(避險)"],"data":[["2330","台積電","1","2","0","0","3","4","5","6","7","8"],["3661","世芯-KY","1,642,098","896,111","0","0","23,000","300,014","47,198","13,300","182,740","146,517"]]}
        margin={"tables":[{"fields":["代號","名稱","買進","賣出","現金償還","前日餘額","今日餘額","次一營業日限額","買進","賣出","現券償還","前日餘額","今日餘額","次一營業日限額","資券互抵","註記"],"data":[["3661","世芯-KY","336","411","0","6,291","6,216","20,877","14","7","0","66","59","20,877","1"," "]]}]}
        inst_rows=server.parse_twse_institutional_payload(institutional,"3661","2026-08-14")
        margin_rows=server.parse_twse_margin_payload(margin,"3661","2026-08-14")
        self.assertEqual(len(inst_rows),1)
        self.assertEqual(inst_rows[0]["Foreign_Investor_buy"],1642098)
        self.assertEqual(inst_rows[0]["Investment_Trust_sell"],300014)
        self.assertEqual(margin_rows[0]["MarginPurchaseTodayBalance"],6216)
        self.assertEqual(margin_rows[0]["ShortSaleTodayBalance"],59)

    def test_parses_official_revenue_in_ntd_with_prior_year_comparison(self):
        payload=[{"資料年月":"11507","公司代號":"3661","營業收入-當月營收":"7433152","營業收入-去年當月營收":"5359000"}]
        rows=server.parse_twse_revenue_rows(payload,"3661")
        self.assertEqual([row["revenue_year"] for row in rows],[2025,2026])
        self.assertEqual(rows[-1]["revenue"],7433152000)
        revenue=server.calc_revenue(rows)
        self.assertEqual(revenue["revenue_period"],"2026-07")
        self.assertAlmostEqual(revenue["revenue_yoy"],(7433152/5359000-1)*100)

    def test_parses_lending_and_sbl_short_sale_in_lots(self):
        short={"data":[["2330","台積電","0","0","0","0","0","0","0","0","0","0","0","0",""],["3661","世芯-KY","66,000","7,000","14,000","0","59,000","20,877,094","1,299,800","16,000","196,000","0","1,119,800","687,444",""]]}
        borrow={"data":[["3661","世芯-KY","3,407,000","89,000","197,000","3,299,000","4,210.00","13,888,790,000","集中市場"]]}
        result=server.parse_twse_lending_snapshots(short,borrow,"3661","2026-08-14")
        self.assertEqual(result["sbl_short_balance"],1119.8)
        self.assertEqual(result["sbl_short_change"],-180)
        self.assertEqual(result["sbl_balance"],3299)
        self.assertEqual(result["sbl_balance_change"],-108)


class FlowAndValuationIntegrityTests(unittest.TestCase):
    def test_calc_flow_exposes_short_balance_changes_and_ratio(self):
        margin=[]
        for i in range(21):
            margin.append({"date":f"2026-07-{i+1:02d}","MarginPurchaseTodayBalance":6000+i*10,"ShortSaleTodayBalance":40+i})
        flow=server.calc_flow([],margin,{"sbl_balance":3299,"sbl_short_balance":1119.8,"lending_last_date":"2026-08-14"})
        self.assertEqual(flow["short_balance"],60)
        self.assertIsNotNone(flow["short_20_pct"])
        self.assertAlmostEqual(flow["short_margin_ratio_pct"],60/6200*100)
        self.assertEqual(flow["sbl_balance"],3299)

    def test_consensus_excludes_unknown_media_and_deduplicates_reposts(self):
        today=server.date.today().isoformat()
        rows=[
            {"institution":"未辨識機構","report_date":today,"target_price":6000,"confidence":65,"source_type":"public_web_quote"},
            {"institution":"摩根士丹利","report_date":today,"target_price":4388,"confidence":95,"source_type":"public_web_quote"},
            {"institution":"摩根士丹利","report_date":today,"target_price":4388,"confidence":95,"source_type":"public_web_quote"},
            {"institution":"高盛","report_date":today,"target_price":5000,"confidence":90,"source_type":"public_web_quote"},
        ]
        research=server.merge_research([],rows)
        self.assertEqual(research["target_coverage"],2)
        self.assertEqual(research["median_target"],4694)
        self.assertEqual(research["market_mention_count"],1)

    def test_model_keeps_trailing_forward_and_analyst_targets_separate(self):
        valuation=server.model_valuation(4210,{"pe_p25":40,"pe_median":48,"pe_p75":67,"sample_count":700},
            {"ttm_eps":72.27,"quarter_period":"2026 Q2"},{"median_forward_eps":160,"eps_coverage":2,"forward_eps_year":2026,"median_target":5000,"target_coverage":2},
            {"core_financials_allowed":True})
        self.assertEqual(valuation["selected_model"],"forward_consensus")
        self.assertAlmostEqual(valuation["trailing_scenarios"][1]["target"],72.27*48)
        self.assertAlmostEqual(valuation["forward_scenarios"][1]["target"],160*48)
        self.assertEqual(valuation["analyst_consensus"]["median_target"],5000)


class TpexPriceParserTests(unittest.TestCase):
    def test_normalizes_full_width_ticker_digits(self):
        self.assertEqual(server.normalize_ticker(" ６４８８ "), "6488")
        self.assertEqual(server.require_numeric_taiwan_ticker("２３３０"), "2330")

    def test_normalizes_tpex_monthly_ohlc_and_converts_lots_to_shares(self):
        payload = {
            "stat": "ok",
            "tables": [{
                "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤", "漲跌", "筆數"],
                "data": [["115/08/14", "859", "2,554,994", "2,830.00", "3,100.00", "2,810.00", "2,990.00", "170.00", "3,536"]],
            }],
        }
        rows = server.parse_tpex_stock_day_payload(payload)
        self.assertEqual(rows, [{
            "date": "2026-08-14", "open": 2830.0, "max": 3100.0,
            "min": 2810.0, "close": 2990.0, "Trading_Volume": 859000.0,
        }])

    def test_rejects_non_ok_payload(self):
        with self.assertRaises(RuntimeError):
            server.parse_tpex_stock_day_payload({"stat": "參數輸入錯誤"})


class MissingValueSemanticsTests(unittest.TestCase):
    def test_unknown_institutional_columns_are_missing_not_zero(self):
        rows = [{"date": "2026-08-14", "stock_id": "6510", "unexpected": "123"}]
        result = server.calc_flow(rows, [])
        self.assertIsNone(result["foreign_20"])
        self.assertIsNone(result["trust_20"])
        self.assertIsNone(result["dealer_20"])
        self.assertNotIn("last_date", result)

    def test_known_zero_net_flow_remains_real_zero(self):
        rows = [{
            "date": "2026-08-14", "stock_id": "6510",
            "Foreign_Investor_buy": "1000", "Foreign_Investor_sell": "1000",
        }]
        result = server.calc_flow(rows, [])
        self.assertEqual(result["foreign_1"], 0.0)
        self.assertEqual(result["last_date"], "2026-08-14")
        self.assertIsNone(result["trust_1"])


class OfficialMarketRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_otc_market_uses_tpex_price_first(self):
        otc_rows = [{"date": "2026-08-14", "open": 1.0, "max": 2.0, "min": 1.0, "close": 2.0, "Trading_Volume": 1000.0}]
        with patch("server.fetch_tpex_stock_day", new=AsyncMock(return_value=(otc_rows, []))) as tpex, \
             patch("server.fetch_twse_stock_day", new=AsyncMock(return_value=([], []))) as twse:
            rows, provider, errors = await server.fetch_official_stock_day("6510", "上櫃")
        self.assertEqual(rows, otc_rows)
        self.assertEqual(provider, "TPEx afterTrading/tradingStock")
        self.assertEqual(errors, [])
        tpex.assert_awaited_once_with("6510", 13)
        twse.assert_not_awaited()

    async def test_official_info_accepts_otc_when_listed_feed_has_no_match(self):
        otc_info = {"stock_id": "6510", "stock_name": "精測", "industry_category": "產業代碼 24", "type": "上櫃"}
        with patch("server.fetch_twse_stock_info", new=AsyncMock(return_value=({}, []))), \
             patch("server.fetch_tpex_stock_info", new=AsyncMock(return_value=(otc_info, []))):
            info, errors = await server.fetch_official_stock_info("6510")
        self.assertEqual(info["stock_name"], "精測")
        self.assertEqual(info["type"], "上櫃")
        self.assertEqual(errors, [])

    async def test_reconciliation_promotes_expected_official_quarter(self):
        stale = {
            "official": True, "period": "2026 Q1", "fiscal_year": 2026,
            "fiscal_quarter": 1, "ytd_eps": 10.43, "source": "older official feed",
            "completeness": 1,
        }
        current = {
            "official": True, "period": "2026 Q2", "fiscal_year": 2026,
            "fiscal_quarter": 2, "ytd_eps": 25.45,
            "source": "MOPS historical income statement summary", "completeness": 1,
        }
        with patch("server.fetch_mops_csv_official", new=AsyncMock(return_value=[])), \
             patch("server.fetch_official_eps_for_period", new=AsyncMock(return_value=current)):
            result = await server.reconcile_official_financial_snapshot("6510", stale)
        self.assertEqual(result["period"], "2026 Q2")
        self.assertEqual(result["ytd_eps"], 25.45)


if __name__ == "__main__":
    unittest.main()
