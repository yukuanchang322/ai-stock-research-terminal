import unittest

import server


class OfficialCompletenessTests(unittest.TestCase):
    def test_revenue_parser_converts_mops_thousands_to_ntd(self):
        rows = server.parse_twse_revenue_rows([
            {"公司代號": "2330", "資料年月": "11507", "營業收入-當月營收": "467580548",
             "營業收入-去年當月營收": "323165707"}
        ], "2330")
        self.assertEqual(rows[-1]["date"], "2026-07-01")
        self.assertEqual(rows[-1]["revenue"], 467580548000)

    def test_flow_requires_actual_number_of_trading_days(self):
        rows = [{"date": f"2026-08-{day:02d}", "Foreign_Investor_buy": 10,
                 "Foreign_Investor_sell": 5} for day in range(1, 6)]
        flow = server.calc_flow(rows, [])
        self.assertEqual(flow["foreign_5"], 25)
        self.assertIsNone(flow["foreign_20"])

    def test_margin_parser_separates_repeated_balance_columns(self):
        payload = {"tables": [{"fields": ["代號", "名稱", "前日餘額", "今日餘額", "前日餘額", "今日餘額"],
                               "data": [["2330", "台積電", "28,000", "27,798", "30", "45"]]}]}
        row = server.parse_twse_margin_payload(payload, "2330", "2026-08-21")[0]
        self.assertEqual(row["MarginPurchaseTodayBalance"], 27798)
        self.assertEqual(row["ShortSaleTodayBalance"], 45)

    def test_roc_and_gregorian_official_dates_are_normalized(self):
        self.assertEqual(server._roc_date("1150821"), "2026-08-21")
        self.assertEqual(server._roc_date("20260821"), "2026-08-21")


if __name__ == "__main__":
    unittest.main()
