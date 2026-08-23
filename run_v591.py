"""V5.9.1 Institutional Recovery.
Adds TWSE OpenAPI T86 first, robust field aliases, and keeps V5.9.0 fallback/history.
"""
from __future__ import annotations
from datetime import date
import run_v590, server

VERSION="5.9.1"
server.app.version=VERSION

num=run_v590.num
pick=run_v590.pick
get_json=run_v590.get_json
merge_cf=run_v590.merge_cf
provider_row=run_v590.provider_row


def code(r):
    return str(pick(r,'Code','證券代號','股票代號','stock_id','StockNo') or '').strip()


def institutional_from_row(r, px):
    fb=num(pick(r,'Foreign_Investor_Buy','ForeignInvestorBuy','外陸資買進股數(不含外資自營商)','外陸資買進股數(不含自營商)','外資及陸資買進股數'))
    fs=num(pick(r,'Foreign_Investor_Sell','ForeignInvestorSell','外陸資賣出股數(不含外資自營商)','外陸資賣出股數(不含自營商)','外資及陸資賣出股數'))
    tb=num(pick(r,'Investment_Trust_Buy','InvestmentTrustBuy','投信買進股數'))
    ts=num(pick(r,'Investment_Trust_Sell','InvestmentTrustSell','投信賣出股數'))
    db1=num(pick(r,'Dealer_self_Buy','DealerSelfBuy','自營商買進股數(自行買賣)'))
    ds1=num(pick(r,'Dealer_self_Sell','DealerSelfSell','自營商賣出股數(自行買賣)'))
    db2=num(pick(r,'Dealer_Hedging_Buy','DealerHedgingBuy','自營商買進股數(避險)'))
    ds2=num(pick(r,'Dealer_Hedging_Sell','DealerHedgingSell','自營商賣出股數(避險)'))
    inst={}
    pairs={'foreign':(fb,fs),'trust':(tb,ts),'dealer':((db1 or 0)+(db2 or 0),(ds1 or 0)+(ds2 or 0))}
    for k,(b,s) in pairs.items():
        if b is not None and s is not None:
            inst[k]={'1':{'buy':b*px if px else None,'sell':s*px if px else None,'net':(b-s)*px if px else b-s,'shares_net':b-s,'days':1}}
    return inst


async def latest_t86_openapi(ticker,d):
    j=await get_json('https://openapi.twse.com.tw/v1/fund/T86',timeout=10)
    rs=j if isinstance(j,list) else []
    r=next((x for x in rs if code(x)==ticker),None)
    if not r:return None,{'status':'empty','rows':len(rs)}
    px=num(d.get('price'))
    inst=institutional_from_row(r,px)
    if not inst:return None,{'status':'empty','rows':len(rs),'detail':'ticker found but fields unmatched'}
    ds=str(pick(r,'Date','日期') or d.get('price_as_of') or date.today().isoformat())[:10]
    return {'institutional':inst,'institutional_rows':1,'institutional_source':'TWSE OpenAPI T86 official','last_date':ds},{'status':'ok','rows':len(rs),'date':ds}


async def build_stock_v591(ticker:str,force_refresh:bool=False):
    d=await run_v590.build_stock_v590(ticker,force_refresh=force_refresh)
    cf=d.get('cashflow') or {}
    if not cf.get('institutional'):
        try:
            x,meta=await latest_t86_openapi(ticker,d)
            d=merge_cf(d,x)
            d.setdefault('provider_status',[]).append(provider_row('TWSE OpenAPI T86',meta.get('status','missing'),meta.get('detail') or f"rows={meta.get('rows')}",meta.get('date')))
        except Exception as e:
            d.setdefault('provider_status',[]).append(provider_row('TWSE OpenAPI T86','error',f'{type(e).__name__}: {str(e)[:120]}'))
    cf=d.get('cashflow') or {}
    for row in d.get('source_status') or []:
        if row.get('name')=='三大法人':
            row.update({'as_of':cf.get('last_date'),'status':'ok' if cf.get('institutional') else 'missing','dataset':cf.get('institutional_source') or 'TWSE T86'})
    try:
        d['scores']=server.scores(d.get('technical') or {},d.get('revenue') or {},d.get('flow') or {},d.get('per') or {},d.get('financial') or {},d.get('research') or {})
    except Exception:pass
    d['version']=VERSION
    return d


app=server.app

@app.get('/api/v591/stock/{ticker}')
async def api_v591(ticker:str,refresh:int=0):
    return await build_stock_v591(ticker.strip(),force_refresh=bool(refresh))

# Preserve the production endpoint used by the existing UI.
for r in list(app.routes):
    if getattr(r,'path',None)=='/api/stock/{ticker}':
        try:app.routes.remove(r)
        except ValueError:pass

@app.get('/api/stock/{ticker}')
async def api_stock_v591(ticker:str,refresh:int=0):
    return await build_stock_v591(ticker.strip(),force_refresh=bool(refresh))
