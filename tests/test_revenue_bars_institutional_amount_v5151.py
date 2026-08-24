import unittest
from unittest.mock import patch

import server


class RevenueBarsInstitutionalAmountTests(unittest.TestCase):
    def test_parses_mops_historical_monthly_revenue_in_ntd(self):
        html="""<html><body><table><tr><td>2330</td><td>台積電</td><td>467,580,548</td><td>0</td></tr></table></body></html>"""
        rows=server.parse_mops_monthly_revenue_html(html.encode("big5"),"2330",2026,7)
        self.assertEqual(rows[0]["revenue"],467_580_548_000)
        self.assertEqual(rows[0]["date"],"2026-07-01")

    def test_revenue_keeps_24_actual_monthly_values(self):
        rows=[]
        for i in range(30):
            year=2024+(i//12)
            month=i%12+1
            rows.append({"revenue_year":year,"revenue_month":month,"revenue":100_000_000+i,"date":f"{year}-{month:02d}-01"})
        result=server.calc_revenue(rows)
        self.assertEqual(len(result["series"]),24)
        self.assertEqual(result["series"][-1]["revenue"],100_000_029)

    def test_flow_amount_is_net_shares_times_each_daily_close(self):
        rows=[]
        prices=[]
        for day in range(1,21):
            date=f"2026-08-{day:02d}"
            rows.append({"date":date,"Foreign_Investor_buy":1000,"Foreign_Investor_sell":400,
                         "Investment_Trust_buy":300,"Investment_Trust_sell":500,
                         "Dealer_buy":250,"Dealer_sell":100})
            prices.append({"date":date,"close":100+day})
        result=server.calc_flow(rows,[],None,prices)
        self.assertEqual(result["foreign_1_amount"],600*120)
        self.assertEqual(result["foreign_5_amount"],600*sum(range(116,121)))
        self.assertEqual(result["trust_20_amount"],-200*sum(range(101,121)))
        self.assertEqual(result["dealer_20_amount"],150*sum(range(101,121)))
        self.assertEqual(result["institutional_amount_method"],"net_shares_x_daily_close")

    def test_amount_is_unavailable_when_a_session_close_is_missing(self):
        rows=[{"date":"2026-08-01","Foreign_Investor_buy":100,"Foreign_Investor_sell":20}]
        result=server.calc_flow(rows,[],None,[])
        self.assertIsNone(result["foreign_1_amount"])
        self.assertNotIn("institutional_amount_method",result)


class IndependentHistoryWarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_revenue_survives_market_history_timeout(self):
        revenue=[{"stock_id":"2330","revenue_year":2026,"revenue_month":7,"revenue":1.0,"date":"2026-07-01"}]
        server._OFFICIAL_HISTORY_CACHE.pop("2330",None)
        server._save_official_history("2330","revenue",revenue)
        with patch.object(server.httpx,"AsyncClient",side_effect=TimeoutError()):
            await server._warm_market_provider("2330","institutional")
        self.assertEqual(server._OFFICIAL_HISTORY_CACHE["2330"][1]["revenue"],revenue)


if __name__ == "__main__":
    unittest.main()
