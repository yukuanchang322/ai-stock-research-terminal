"""V5.5.7 Revenue Amount Bars + Robust Margin Parser.

- Revenue chart uses the latest 12 monthly revenue amounts as bar heights.
- YoY is auxiliary tooltip/text only.
- Margin/short parsing is broadened for TWSE MI_MARGN payload/table variants.
"""
from __future__ import annotations
import asyncio
from typing import Any
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import run_v556, run_v551, server

VERSION="5.5.7"
server.app.version=VERSION
_base_build=server.build_stock


def _num(v:Any):
    try:
        if v is None:return None
        s=str(v).replace(',','').replace('--','').strip()
        return float(s) if s else None
    except:return None


def _field_name(x:Any)->str:
    if isinstance(x,str):return x.replace(' ','')
    if isinstance(x,dict):return ''.join(str(v) for v in x.values()).replace(' ','')
    if isinstance(x,(list,tuple)):return ''.join(_field_name(v) for v in x)
    return str(x).replace(' ','')


def _extract_margin_row(payload:dict[str,Any],ticker:str):
    groups=[]
    if isinstance(payload,dict):
        groups.append((payload.get('fields') or [],payload.get('data') or []))
        for t in payload.get('tables') or []:
            if isinstance(t,dict):groups.append((t.get('fields') or [],t.get('data') or []))
    for fields,data in groups:
        names=[_field_name(f) for f in fields]
        for row in data:
            if not isinstance(row,(list,tuple)):continue
            vals=[str(v).strip() for v in row]
            if ticker not in vals:continue
            mb=sb=None
            for i,name in enumerate(names):
                if i>=len(row):continue
                if '融資' in name and ('今日餘額' in name or '餘額' in name):
                    n=_num(row[i]); mb=n if n is not None else mb
                if '融券' in name and ('今日餘額' in name or '餘額' in name):
                    n=_num(row[i]); sb=n if n is not None else sb
            # fallback for layouts where group labels are omitted in field text
            if mb is None or sb is None:
                joined='|'.join(names)
                for i,name in enumerate(names):
                    if i>=len(row):continue
                    if mb is None and ('資今日餘額' in name or name.endswith('融資餘額')): mb=_num(row[i])
                    if sb is None and ('券今日餘額' in name or name.endswith('融券餘額')): sb=_num(row[i])
            if mb is not None or sb is not None:
                return {'margin_balance':mb,'short_balance':sb}
    return None

_sem=asyncio.Semaphore(3)
async def _margin_day(client:httpx.AsyncClient,ticker:str,ds:str):
    async with _sem:
        for sel in ('ALL','ALLBUT0999','MS'):
            try:
                r=await client.get('https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN',params={'date':ds.replace('-',''),'selectType':sel,'response':'json'},timeout=18)
                if r.status_code>=400:continue
                j=r.json()
                if not isinstance(j,dict):continue
                row=_extract_margin_row(j,ticker)
                if row:
                    row['date']=ds; row['source']='TWSE MI_MARGN'; return row
            except Exception:
                continue
    return None


def _chg(hist,n,key):
    if len(hist)<=n:return None
    now=hist[-1].get(key); old=hist[-1-n].get(key)
    return None if now is None or old in (None,0) else (now/old-1)*100

async def _repair_margin(ticker:str,d:dict[str,Any]):
    tech=[x for x in (d.get('technical') or {}).get('series',[]) if x.get('date')][-25:]
    if len(tech)<2:return d
    dates=[str(x['date']) for x in tech[-21:]]
    async with httpx.AsyncClient(follow_redirects=True,headers={'User-Agent':'Mozilla/5.0 AI-Stock-Research/5.5.7'}) as c:
        rows=await asyncio.gather(*(_margin_day(c,ticker,ds) for ds in dates))
    hist=[x for x in rows if x]
    if len(hist)>=2:
        hist=sorted(hist,key=lambda x:x['date'])
        ms={}
        for n in (1,5,20):
            ms[str(n)]={'margin_pct':_chg(hist,n,'margin_balance'),'short_pct':_chg(hist,n,'short_balance'),'margin_balance':hist[-1].get('margin_balance'),'short_balance':hist[-1].get('short_balance')}
        if any(v.get('margin_pct') is not None or v.get('short_pct') is not None for v in ms.values()):
            cf=d.get('cashflow') or {}; cf['margin_short']=ms; cf['margin_short_rows']=len(hist); cf['margin_short_as_of']=hist[-1]['date']; cf['margin_short_source']='TWSE MI_MARGN'; d['cashflow']=cf
    return d

async def build_stock_v557(ticker:str,force_refresh:bool=False):
    d=await _base_build(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try:d=await _repair_margin(ticker,d)
        except Exception:pass
        d['version']=VERSION
        d['data_policy']=(d.get('data_policy') or '')+' V5.5.7：營收柱高改為實際月營收；YoY 僅作輔助資訊；TWSE 融資融券欄位解析加強。'
    return d
server.build_stock=build_stock_v557

_UI=r'''
function v557Revenue(r){
 const data=(r?.series||[]).filter(x=>x&&x.period&&Number.isFinite(Number(x.revenue))).slice(-12);
 if(!data.length)return '<div class="empty bordered">月營收資料不足</div>';
 const w=720,h=230,pl=44,pr=12,pt=24,pb=38,vals=data.map(x=>Number(x.revenue)),mx=Math.max(...vals)||1,step=(w-pl-pr)/data.length,bw=Math.max(12,step*.60);
 const yoyMap={};(r?.yoy_series||[]).forEach(x=>{if(x?.period)yoyMap[x.period]=Number(x.yoy)});
 const bars=data.map((x,i)=>{const v=Number(x.revenue),cx=pl+(i+.5)*step,bh=(h-pt-pb)*(v/mx),yy=h-pb-bh,yoy=yoyMap[x.period],tip=`${x.period} 月營收 ${v.toLocaleString('zh-TW')}${Number.isFinite(yoy)?`｜YoY ${yoy>=0?'+':''}${yoy.toFixed(1)}%`:''}`;return `<g><rect class="revenue-bar positive" x="${cx-bw/2}" y="${yy}" width="${bw}" height="${Math.max(2,bh)}" rx="3"><title>${tip}</title></rect><text class="revenue-xlabel" x="${cx}" y="${h-12}" text-anchor="middle">${x.period.slice(5)}</text></g>`}).join('');
 const last=data[data.length-1],lastY=yoyMap[last.period];return `<div class="revenue-yoy-head"><b>最近 12 個月月營收</b><span>${data.length}/12 月${Number.isFinite(lastY)?`｜最新 YoY ${lastY>=0?'+':''}${lastY.toFixed(1)}%`:''}</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${pl}" y1="${h-pb}" x2="${w-pr}" y2="${h-pb}"/>${bars}</svg><small class="chart-note">柱高＝實際月營收；月份由左至右。YoY 僅作輔助資訊。</small>`;
}
'''
_oldjs=run_v551._patched_app_js
def _js557():
    t=_oldjs()
    if 'function v557Revenue' not in t:t=_UI+'\n'+t
    for old in ["$('fundChart').innerHTML=v556Revenue(r);","$('fundChart').innerHTML=revenueBarSvgCore(r.series||[]);"]:
        t=t.replace(old,"$('fundChart').innerHTML=v557Revenue(r);")
    return t
run_v551._patched_app_js=_js557
_oldidx=run_v551._patched_index
def _idx557():return _oldidx().replace('5.5.6','5.5.7').replace('5.5.1','5.5.7')
run_v551._patched_index=_idx557
_oldsw=run_v551._patched_sw
def _sw557():return _oldsw().replace('ai-stock-v5.5.6','ai-stock-v5.5.7').replace('ai-stock-v5.5.1','ai-stock-v5.5.7')
run_v551._patched_sw=_sw557

@server.app.middleware('http')
async def v557_runtime(request:Request,call_next):
    if request.url.path=='/health':return JSONResponse({'status':'ok','version':VERSION,'mode':'revenue-amount-bars+robust-margin-parser'},headers={'Cache-Control':'no-store'})
    resp=await call_next(request);resp.headers['X-AI-Stock-Version']=VERSION;return resp
app=server.app
