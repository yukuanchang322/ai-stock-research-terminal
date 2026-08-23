"""V5.5.1 Core UI + EPS Integrity Repair.

This runtime removes the dependency on late UI hotfix timing. It serves patched core
HTML/JS/CSS/SW directly and performs a second official EPS resolution pass for the
latest five fiscal quarters before valuation is finalized.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response, JSONResponse

import run_v550
import server

VERSION = "5.5.1"
server.app.version = VERSION
ROOT = Path(__file__).resolve().parent
_base_build_stock = server.build_stock


def _num(v: Any):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _periods(year: int, quarter: int, n: int = 5):
    out=[]; y=int(year); q=int(quarter)
    for _ in range(n):
        out.append((y,q)); q-=1
        if q==0: q=4; y-=1
    return out


async def _resolve_period(ticker: str, y: int, q: int):
    try:
        return await server.fetch_official_eps_for_period(ticker,y,q)
    except Exception:
        return None


def _pick_direct(p: dict[str,Any] | None):
    if not p: return None
    for k in ("quarter_eps","quarter_eps_direct","eps_quarter","single_quarter_eps"):
        v=_num(p.get(k))
        if v is not None: return v
    return None


def _pick_ytd(p: dict[str,Any] | None):
    if not p: return None
    for k in ("ytd_eps","eps_ytd","cumulative_eps"):
        v=_num(p.get(k))
        if v is not None: return v
    return None


async def _repair_eps(ticker: str, d: dict[str,Any]):
    off=d.get("official_financial") or {}
    fy=off.get("fiscal_year"); fq=off.get("fiscal_quarter")
    if not fy or not fq:
        return d
    periods=_periods(int(fy),int(fq),5)
    resolved=await asyncio.gather(*(_resolve_period(ticker,y,q) for y,q in periods))
    by={(y,q):p for (y,q),p in zip(periods,resolved) if isinstance(p,dict)}

    # Merge existing usable evidence first; resolver results may improve missing rows.
    stack=d.get("eps_stack") or {}
    existing={}
    for row in stack.get("evidence_ledger") or []:
        try: existing[(int(row.get("year")),int(row.get("quarter")))]=row
        except Exception: pass

    ytd_map={}
    direct_map={}
    source_map={}
    for y,q in periods:
        p=by.get((y,q)) or {}
        old=existing.get((y,q)) or {}
        ytd=_pick_ytd(p)
        if ytd is None: ytd=_num(old.get("ytd_eps"))
        direct=_pick_direct(p)
        if direct is None: direct=_num(old.get("quarter_eps_direct"))
        if direct is None: direct=_num(old.get("derived_quarter_eps")) if old.get("status")=="usable" else None
        if ytd is not None: ytd_map[(y,q)]=ytd
        if direct is not None: direct_map[(y,q)]=direct
        source_map[(y,q)]=(p.get("source") or old.get("source") or p.get("endpoint") or old.get("source_url"))

    quarter_map={}
    method_map={}
    # Oldest -> newest so cumulative differences are deterministic.
    for y,q in reversed(periods):
        if (y,q) in direct_map:
            quarter_map[(y,q)]=direct_map[(y,q)]; method_map[(y,q)]="official_direct"
            continue
        ytd=ytd_map.get((y,q))
        if ytd is None: continue
        if q==1:
            quarter_map[(y,q)]=ytd; method_map[(y,q)]="official_ytd_q1"
        else:
            prev=ytd_map.get((y,q-1))
            if prev is not None:
                quarter_map[(y,q)]=ytd-prev; method_map[(y,q)]="official_ytd_difference"

    ledger=[]
    for y,q in periods:
        p=by.get((y,q)) or {}; ytd=ytd_map.get((y,q)); qe=quarter_map.get((y,q))
        usable=qe is not None or ytd is not None
        ledger.append({
            "period":f"{y} Q{q}","year":y,"quarter":q,"status":"usable" if usable else "missing",
            "evidence_type":"quarter_eps_direct" if method_map.get((y,q))=="official_direct" else ("ytd_eps" if ytd is not None else "missing"),
            "quarter_eps_direct":direct_map.get((y,q)),"ytd_eps":ytd,
            "derived_quarter_eps":qe,"derivation_method":method_map.get((y,q)),
            "source":source_map.get((y,q)) or "missing_official",
            "source_url":p.get("endpoint") or p.get("source_url"),
            "missing_reason":None if usable else "missing_official",
        })

    latest=periods[0]
    latest_q=quarter_map.get(latest)
    latest_ytd=ytd_map.get(latest)
    latest4=periods[:4]
    ttm=sum(quarter_map[k] for k in latest4) if all(k in quarter_map for k in latest4) else None
    stack.update({
        "quarter_period":f"{latest[0]} Q{latest[1]}",
        "quarter_eps":latest_q,
        "ytd_eps":latest_ytd if latest_ytd is not None else stack.get("ytd_eps"),
        "ttm_eps":ttm,
        "evidence_ledger":ledger,
        "evidence_ledger_version":VERSION,
        "historical_backfill":{
            "attempted_periods":[x["period"] for x in ledger[1:]],
            "resolved_periods":[x["period"] for x in ledger[1:] if x["status"]=="usable"],
            "missing_periods":[x["period"] for x in ledger[1:] if x["status"]!="usable"],
            "policy":"exact-period official resolver; direct quarterly EPS preferred; otherwise official cumulative EPS difference",
        },
        "note":"V5.5.1：最近五季逐季重新查核官方 EPS；TTM 僅在連續四個單季都有可稽核證據時才計算。",
    })
    d["eps_stack"]=stack
    fin=d.get("financial") or {}
    fin["quarter_eps"]=latest_q; fin["ttm_eps"]=ttm
    if latest_ytd is not None: fin["ytd_eps"]=latest_ytd
    d["financial"]=fin

    # Recompute all price/EPS-sensitive outputs after repaired evidence.
    integrity=d.get("financial_integrity") or {}
    d["valuation"]=server.model_valuation(d.get("price"),d.get("per") or {},stack,d.get("research") or {},integrity)
    try:
        sc=server.scores(d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("per") or {},fin,d.get("research") or {})
        d["scores"]=sc
        nar=server.narrative(sc,d.get("technical") or {},d.get("revenue") or {},d.get("flow") or {},d.get("valuation") or {},d.get("research") or {})
        d["stance"]=nar.get("stance",d.get("stance")); d["thesis"]=nar.get("thesis",d.get("thesis")); d["catalysts"]=nar.get("catalysts",d.get("catalysts")); d["risks"]=nar.get("risks",d.get("risks"))
    except Exception:
        pass
    return d


async def build_stock_v551(ticker: str, force_refresh: bool=False):
    d=await _base_build_stock(ticker,force_refresh=force_refresh)
    if isinstance(d,dict):
        try: d=await _repair_eps(ticker,d)
        except Exception: pass
        d["version"]=VERSION
        d["data_policy"]=(d.get("data_policy") or "")+" V5.5.1：營收與籌碼 UI 進入核心渲染；最近五季 EPS 逐期官方補查，TTM 僅使用連續四季可稽核單季 EPS。"
    return d

server.build_stock=build_stock_v551


CORE_JS = r'''
function revenueYoYCore(series){
  const rows=(series||[]).filter(x=>x&&x.period&&Number.isFinite(Number(x.revenue)));
  const m=new Map(rows.map(x=>[String(x.period),Number(x.revenue)]));
  return rows.map(x=>{const z=String(x.period).match(/^(\d{4})-(\d{2})$/);if(!z)return null;const b=m.get(`${Number(z[1])-1}-${z[2]}`);return Number.isFinite(b)&&b!==0?{period:String(x.period),yoy:(Number(x.revenue)/b-1)*100}:null}).filter(Boolean).slice(-12);
}
function revenueBarSvgCore(series){
  const data=revenueYoYCore(series);if(!data.length)return '<div class="empty">營收年增資料不足</div>';
  const w=720,h=220,pl=46,pr=12,pt=16,pb=34,vals=data.map(x=>x.yoy),mn=Math.min(0,...vals),mx=Math.max(0,...vals),span=(mx-mn)||1;
  const y=v=>pt+(mx-v)*(h-pt-pb)/span,zero=y(0),step=(w-pl-pr)/data.length,bw=Math.max(12,step*.62);
  const bars=data.map((x,i)=>{const cx=pl+(i+.5)*step,yy=y(x.yoy),top=Math.min(yy,zero),bh=Math.max(1,Math.abs(yy-zero));return `<g><rect class="revenue-bar ${x.yoy>=0?'positive':'negative'}" x="${cx-bw/2}" y="${top}" width="${bw}" height="${bh}" rx="3"><title>${x.period} YoY ${x.yoy>=0?'+':''}${x.yoy.toFixed(1)}%</title></rect><text class="revenue-xlabel" x="${cx}" y="${h-10}" text-anchor="middle">${x.period.slice(5)}</text></g>`}).join('');
  const last=data[data.length-1];return `<div class="revenue-yoy-head"><b>近 12 月營收年增率</b><span>最新 ${last.yoy>=0?'+':''}${last.yoy.toFixed(1)}%</span></div><svg class="revenue-bar-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><line class="revenue-zero" x1="${pl}" y1="${zero}" x2="${w-pr}" y2="${zero}"/>${bars}<text class="revenue-axis-label" x="4" y="${pt+8}">${mx.toFixed(0)}%</text><text class="revenue-axis-label" x="8" y="${Math.max(pt+10,Math.min(h-pb,zero+4))}">0%</text>${mn<0?`<text class="revenue-axis-label" x="4" y="${h-pb}">${mn.toFixed(0)}%</text>`:''}</svg><small class="chart-note">每根柱代表該月營收相較去年同月；綠色成長、紅色衰退。</small>`;
}
function compactFlowCore(v){if(v==null||!Number.isFinite(Number(v)))return '—';const n=Number(v),a=Math.abs(n),s=n>0?'+':n<0?'-':'';if(a>=1e8)return `${s}${(a/1e8).toFixed(a>=1e9?1:2)}億`;if(a>=1e4)return `${s}${(a/1e4).toFixed(a>=1e7?0:1)}萬`;return `${s}${Math.round(a).toLocaleString('zh-TW')}`;}
'''

NEW_FLOW = r'''function flowMatrix(fl){
  const rows=[['外資','foreign'],['投信','trust'],['自營商','dealer']];
  const cell=v=>`<span class="${Number(v)<0?'neg':'pos'}" title="${v==null?'—':Number(v).toLocaleString('zh-TW')}">${compactFlowCore(v)}</span>`;
  return `<div class="flow-matrix"><div class="flow-head"><b>法人</b><b>1日</b><b>5日</b><b>20日</b></div>${rows.map(([label,key])=>`<div class="flow-matrix-row"><b>${label}</b>${[1,5,20].map(n=>cell(fl?.[`${key}_${n}`])).join('')}</div>`).join('')}<div class="flow-matrix-row margin-row"><b>融資%</b>${[1,5,20].map(n=>{const v=fl?.[`margin_${n}_pct`];return `<span class="${Number(v)>0?'neg':'pos'}">${v==null?'—':`${Number(v)>0?'+':''}${Number(v).toFixed(1)}%`}</span>`}).join('')}</div></div>`;
}
'''

CORE_CSS = r'''
/* V5.5.1 core mobile data visualization */
#fundChart{overflow:hidden}.revenue-yoy-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:2px 2px 8px;color:var(--muted);font-size:11px}.revenue-yoy-head b{color:var(--text);font-size:12px}.revenue-bar-svg{display:block;width:100%;height:210px}.revenue-zero{stroke:#405461;stroke-width:1}.revenue-bar.positive{fill:var(--accent)}.revenue-bar.negative{fill:var(--red)}.revenue-xlabel,.revenue-axis-label{fill:var(--muted);font-size:10px}.chart-note{display:block;color:var(--muted);font-size:10px;margin-top:6px}
#flowTable{width:100%;min-width:0;overflow:visible}.flow-matrix{width:100%;max-width:none!important;min-width:0;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#0a171f}.flow-head,.flow-matrix-row{display:grid!important;grid-template-columns:52px repeat(3,minmax(0,1fr))!important;width:100%!important;min-width:0!important}.flow-head>* ,.flow-matrix-row>*{min-width:0!important;padding:11px 5px;border-right:1px solid var(--line);display:flex;align-items:center;justify-content:flex-end;overflow:hidden}.flow-head>*:last-child,.flow-matrix-row>*:last-child{border-right:0}.flow-head{border-bottom:1px solid var(--line)}.flow-matrix-row{border-bottom:1px solid var(--line)}.flow-matrix-row:last-child{border-bottom:0}.flow-head b:first-child,.flow-matrix-row>b{justify-content:flex-start}.flow-matrix-row span{font-size:11px;font-weight:800;white-space:nowrap}.flow-matrix-row .pos{color:var(--accent)}.flow-matrix-row .neg{color:var(--red)}
@media(max-width:900px){.two-col>div,.two-col .panel{min-width:0;width:100%;overflow:visible}.revenue-bar-svg{height:190px}.flow-head,.flow-matrix-row{grid-template-columns:44px repeat(3,minmax(0,1fr))!important}.flow-head>* ,.flow-matrix-row>*{padding:10px 3px}.flow-matrix-row span{font-size:10px;letter-spacing:-.03em}.flow-matrix-row>b,.flow-head b{font-size:10px}}
'''


def _patched_app_js():
    text=(ROOT/"app.js").read_text(encoding="utf-8")
    if "revenueYoYCore" not in text:
        text=CORE_JS+"\n"+text
    text=re.sub(r"function flowMatrix\(fl\)\{.*?\n\}\n(?=function _techLinePoints)",NEW_FLOW,text,count=1,flags=re.S)
    text=text.replace("$('fundChart').innerHTML=lineSvg((r.series||[]).map(x=>x.revenue));","$('fundChart').innerHTML=revenueBarSvgCore(r.series||[]); const _rh=$('fundChart')?.previousElementSibling; if(_rh&&_rh.tagName==='H4')_rh.textContent='近 12 月營收成長 YoY';")
    return text


def _patched_index():
    text=(ROOT/"index.html").read_text(encoding="utf-8")
    text=re.sub(r"AI Stock Research Terminal V[0-9.]+",f"AI Stock Research Terminal V{VERSION}",text)
    text=text.replace("近 24 月營收","近 12 月營收成長 YoY")
    text=re.sub(r"styles\.css\?v=[0-9.]+",f"styles.css?v={VERSION}",text)
    text=re.sub(r"recovery\.js\?v=[0-9.]+",f"recovery.js?v={VERSION}",text)
    text=re.sub(r"app\.js\?v=[0-9.]+",f"app.js?v={VERSION}",text)
    # Remove the legacy late hotfix entirely: V5.5.1 core app.js owns rendering.
    text=re.sub(r"\s*<script src=\"v547_hotfix\.js\?v=[^\"]+\"></script>","",text)
    text=text.replace("V5.4.7 延續 Multi-Source Evidence Engine","V5.5.1 延續 Multi-Source Evidence Engine")
    return text


def _patched_sw():
    text=(ROOT/"sw.js").read_text(encoding="utf-8")
    text=re.sub(r'const CACHE="[^"]+";',f'const CACHE="ai-stock-v5.5.1";',text)
    text=text.replace(", '/v547_hotfix.js'","").replace("'/v547_hotfix.js', ","")
    return text


@server.app.middleware("http")
async def v551_core_runtime(request: Request, call_next):
    path=request.url.path
    if path=="/health":
        return JSONResponse({"status":"ok","version":VERSION,"mode":"core-ui+eps-integrity+price-integrity","core_revenue_bars":True,"core_flow_matrix":True,"five_quarter_eps_repair":True,"pwa":True},headers={"Cache-Control":"no-store"})
    if path=="/":
        return HTMLResponse(_patched_index(),headers={"Cache-Control":"no-store, max-age=0"})
    if path=="/app.js":
        return Response(_patched_app_js(),media_type="application/javascript",headers={"Cache-Control":"no-store, max-age=0","X-AI-Stock-Version":VERSION})
    if path=="/styles.css":
        text=(ROOT/"styles.css").read_text(encoding="utf-8")+"\n"+CORE_CSS
        return Response(text,media_type="text/css",headers={"Cache-Control":"no-store, max-age=0"})
    if path=="/sw.js":
        return Response(_patched_sw(),media_type="application/javascript",headers={"Cache-Control":"no-store, max-age=0","Service-Worker-Allowed":"/"})
    response=await call_next(request)
    response.headers["X-AI-Stock-Version"]=VERSION
    return response

app=server.app
