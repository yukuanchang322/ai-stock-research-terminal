from __future__ import annotations

import asyncio
import html
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import quote_plus
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "generated_reports"
DATA_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

app = FastAPI(title="AI Stock Research Terminal", version="5.1.2")
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory=ROOT), name="static")


def safe_num(v: Any, default: float | None = None) -> float | None:
    try:
        if v in (None, "", "--", "—"):
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def nfmt(v: float | int | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.{digits}f}"


async def finmind(dataset: str, ticker: str | None = None, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"dataset": dataset}
    if ticker:
        params["data_id"] = ticker
    if start:
        params["start_date"] = start.isoformat()
    if end:
        params["end_date"] = end.isoformat()
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        r = await client.get(FINMIND_BASE, params=params, headers=headers)
        r.raise_for_status()
        payload = r.json()
    if payload.get("status") not in (200, None):
        raise RuntimeError(payload.get("msg") or f"FinMind {dataset} failed")
    return payload.get("data") or []




ANALYST_ALIASES = {
    "摩根士丹利": ["摩根士丹利", "大摩", "Morgan Stanley"],
    "摩根大通": ["摩根大通", "小摩", "JPMorgan", "JP Morgan"],
    "高盛": ["高盛", "Goldman Sachs"],
    "花旗": ["花旗", "Citi", "Citigroup"],
    "瑞銀": ["瑞銀", "UBS"],
    "美銀": ["美銀", "Bank of America", "BofA"],
    "野村": ["野村", "Nomura"],
    "麥格理": ["麥格理", "Macquarie"],
    "匯豐": ["匯豐", "HSBC"],
    "里昂": ["里昂", "CLSA"],
    "元大": ["元大"], "凱基": ["凱基"], "富邦": ["富邦"], "永豐": ["永豐"],
    "國泰": ["國泰"], "群益": ["群益"], "統一": ["統一"], "元富": ["元富"],
}
RATING_MAP = [
    ("買進", ["買進", "Buy", "加碼", "優於大盤", "Overweight", "Outperform"]),
    ("中立", ["中立", "Neutral", "持有", "Hold", "Equal-weight", "Market Perform"]),
    ("賣出", ["賣出", "Sell", "減碼", "Underweight", "Underperform"]),
]

def _extract_institution(text: str) -> str | None:
    low=text.lower()
    for canonical, aliases in ANALYST_ALIASES.items():
        if any(a.lower() in low for a in aliases): return canonical
    return None

def _extract_rating(text: str) -> str | None:
    low=text.lower()
    for canonical, words in RATING_MAP:
        if any(w.lower() in low for w in words): return canonical
    return None

def _extract_target(text: str) -> float | None:
    patterns=[
        r"目標價(?:調升|上調|上看|調降|下調|降至|升至|至|為|看|[:：])?\s*(?:新台幣|NT\$?|TWD)?\s*([0-9]{2,5}(?:\.[0-9]+)?)",
        r"target price(?: raised| lowered| to| of|[:：])?\s*(?:NT\$?|TWD)?\s*([0-9]{2,5}(?:\.[0-9]+)?)",
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            v=safe_num(m.group(1))
            if v and 5 <= v <= 100000: return v
    return None

def _extract_eps(text: str) -> float | None:
    pats=[r"(?:EPS|每股盈餘)[^0-9]{0,16}([0-9]{1,4}(?:\.[0-9]+)?)", r"([0-9]{1,4}(?:\.[0-9]+)?)\s*元[^。；,，]{0,8}(?:EPS|每股盈餘)"]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            v=safe_num(m.group(1))
            if v and 0 < v < 5000: return v
    return None

def _normalize_title(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s).lower()

async def google_news_rss(query: str) -> list[dict[str, Any]]:
    url=f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    async with httpx.AsyncClient(timeout=18, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.1"}) as client:
        r=await client.get(url); r.raise_for_status()
    root=ET.fromstring(r.text)
    out=[]
    for item in root.findall('.//item')[:30]:
        title=html.unescape(item.findtext('title') or '').strip()
        link=(item.findtext('link') or '').strip()
        desc=html.unescape(item.findtext('description') or '').strip()
        desc=re.sub('<[^>]+>',' ',desc)
        pub=item.findtext('pubDate') or ''
        try: pub_date=parsedate_to_datetime(pub).date().isoformat()
        except Exception: pub_date=''
        source_el=item.find('source'); source=(source_el.text or '').strip() if source_el is not None else ''
        out.append({"title":title,"url":link,"snippet":re.sub(r'\s+',' ',desc).strip()[:420],"published_date":pub_date,"publisher":source})
    return out

async def fetch_company_events(ticker: str, company_name: str) -> dict[str, Any]:
    queries=[f'"{company_name}" {ticker} 法說 展望', f'"{company_name}" {ticker} 重大訊息 營收 財報', f'"{company_name}" {ticker} 接單 產能 客戶']
    results=await asyncio.gather(*(google_news_rss(q) for q in queries), return_exceptions=True)
    rows=[]; seen=set(); errors=[]
    for result in results:
        if isinstance(result, Exception): errors.append(type(result).__name__); continue
        for x in result:
            key=_normalize_title(x['title'])[:90]
            if not key or key in seen: continue
            seen.add(key)
            text=f"{x['title']} {x['snippet']}"
            tags=[]
            for tag, words in [("法說",["法說","法人說明會"]),("財報",["財報","獲利","EPS"]),("營收",["營收"]),("展望",["展望","上修","下修","看旺","看淡"]),("重大訊息",["重大訊息","公告"]),("營運",["接單","產能","客戶","訂單"])]:
                if any(w.lower() in text.lower() for w in words): tags.append(tag)
            if not tags: continue
            rows.append({"date":x['published_date'],"title":x['title'],"summary":x['snippet'][:240],"publisher":x['publisher'],"source_url":x['url'],"tags":tags[:3]})
    return {"rows":sorted(rows,key=lambda z:z.get('date',''),reverse=True)[:12],"errors":errors,"fetched_at":datetime.now().astimezone().isoformat(timespec='seconds')}

async def fetch_public_research(ticker: str, company_name: str) -> dict[str, Any]:
    queries=[
        f'"{company_name}" {ticker} 目標價 法人 券商',
        f'"{company_name}" {ticker} EPS 上修 下修 法人',
        f'"{company_name}" {ticker} 法說 目標價 投資評等',
    ]
    rows=[]; errors=[]
    results=await asyncio.gather(*(google_news_rss(q) for q in queries), return_exceptions=True)
    seen=set()
    for result in results:
        if isinstance(result, Exception): errors.append(type(result).__name__); continue
        for x in result:
            key=_normalize_title(x['title'])[:90]
            if not key or key in seen: continue
            seen.add(key)
            text=f"{x['title']} {x['snippet']}"
            inst=_extract_institution(text); target=_extract_target(text); rating=_extract_rating(text); eps=_extract_eps(text)
            if not any([inst,target,rating,eps]): continue
            score=35 + (25 if inst else 0) + (25 if target else 0) + (10 if rating else 0) + (5 if eps else 0)
            rows.append({
                "institution":inst or "未辨識機構", "report_date":x['published_date'], "rating":rating,
                "target_price":target, "forward_eps":eps, "title":x['title'], "summary":x['snippet'][:220],
                "publisher":x['publisher'], "source_url":x['url'], "source_type":"public_web_quote",
                "confidence":min(100,score), "copyright_note":"僅保存公開標題/摘要/數值與來源連結，不重製付費研究全文。"
            })
    rows=sorted(rows,key=lambda z:(z.get('report_date') or '', z.get('confidence') or 0),reverse=True)[:20]
    return {"rows":rows,"errors":errors,"queries":queries,"fetched_at":datetime.now().astimezone().isoformat(timespec='seconds')}

def merge_research(imported: list[dict[str, Any]], web_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows=[]
    for x in imported:
        y=dict(x); y.setdefault('source_type','manual_import'); y.setdefault('confidence',95); rows.append(y)
    rows.extend(web_rows)
    rows=sorted(rows,key=lambda x:x.get('report_date',''),reverse=True)
    targets=[safe_num(x.get('target_price')) for x in rows if safe_num(x.get('target_price')) is not None]
    epss=[safe_num(x.get('forward_eps')) for x in rows if safe_num(x.get('forward_eps')) is not None]
    institutions={x.get('institution') for x in rows if x.get('institution') and x.get('institution')!='未辨識機構'}
    ratings={"買進":0,"中立":0,"賣出":0}
    for x in rows:
        r=x.get('rating')
        if r in ratings: ratings[r]+=1
    revisions=[]; eps_revisions=[]
    by_inst={}; by_inst_eps={}
    for x in rows:
        inst=x.get('institution'); tp=safe_num(x.get('target_price')); ep=safe_num(x.get('forward_eps'))
        if inst and inst!='未辨識機構':
            if tp is not None: by_inst.setdefault(inst,[]).append((x.get('report_date',''),tp))
            if ep is not None: by_inst_eps.setdefault(inst,[]).append((x.get('report_date',''),ep))
    for vals in by_inst.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: revisions.append((vals[0][1]/vals[1][1]-1)*100)
    for vals in by_inst_eps.values():
        vals=sorted(vals,reverse=True)
        if len(vals)>=2 and vals[1][1]: eps_revisions.append((vals[0][1]/vals[1][1]-1)*100)
    return {
        "count":len(rows), "institution_count":len(institutions), "median_target":median(targets) if targets else None,
        "average_target":sum(targets)/len(targets) if targets else None, "high_target":max(targets) if targets else None, "low_target":min(targets) if targets else None,
        "median_forward_eps":median(epss) if epss else None, "target_revision_pct":median(revisions) if revisions else None, "eps_revision_pct":median(eps_revisions) if eps_revisions else None,
        "ratings":ratings, "reports":rows, "public_web_count":sum(1 for x in rows if x.get('source_type')=='public_web_quote'),
        "manual_count":sum(1 for x in rows if x.get('source_type')=='manual_import'),
    }


def expectation_gap_analysis(research: dict[str, Any], events: dict[str, Any], perdata: dict[str, Any], revenue: dict[str, Any], scores_map: dict[str, int], price: float | None) -> dict[str, Any]:
    """V5.1: combine estimate revisions, analyst target revisions, event tone and valuation stretch.
    This is a transparent signal framework, not a price forecast.
    """
    reports = research.get("reports") or []
    by_inst: dict[str, list[dict[str, Any]]] = {}
    for row in reports:
        inst = row.get("institution")
        if not inst or inst == "未辨識機構":
            continue
        by_inst.setdefault(inst, []).append(row)
    revision_rows = []
    for inst, rows in by_inst.items():
        rows = sorted(rows, key=lambda x: x.get("report_date") or "", reverse=True)
        if len(rows) < 2:
            continue
        newest, older = rows[0], rows[1]
        nt, ot = safe_num(newest.get("target_price")), safe_num(older.get("target_price"))
        ne, oe = safe_num(newest.get("forward_eps")), safe_num(older.get("forward_eps"))
        tr = ((nt / ot) - 1) * 100 if nt is not None and ot else None
        er = ((ne / oe) - 1) * 100 if ne is not None and oe else None
        if tr is None and er is None:
            continue
        revision_rows.append({
            "institution": inst, "latest_date": newest.get("report_date"), "previous_date": older.get("report_date"),
            "latest_target": nt, "previous_target": ot, "target_revision_pct": tr,
            "latest_eps": ne, "previous_eps": oe, "eps_revision_pct": er,
            "latest_rating": newest.get("rating"), "source_url": newest.get("source_url")
        })

    positive_words = ["上修","調升","看旺","優於預期","成長","強勁","創高","增加","改善","樂觀","需求旺","接單","擴產"]
    negative_words = ["下修","調降","看淡","不如預期","衰退","疲弱","減少","惡化","保守","庫存","延後","砍單"]
    tone_points = 0
    recent_count = 0
    for row in (events.get("rows") or [])[:12]:
        text = f"{row.get('title','')} {row.get('summary','')}"
        pos = sum(1 for w in positive_words if w in text)
        neg = sum(1 for w in negative_words if w in text)
        tone_points += pos - neg
        recent_count += 1
    event_tone_score = max(-100, min(100, tone_points * 18)) if recent_count else 0

    eps_rev = safe_num(research.get("eps_revision_pct"))
    target_rev = safe_num(research.get("target_revision_pct"))
    median_target = safe_num(research.get("median_target"))
    target_upside = ((median_target / price) - 1) * 100 if median_target is not None and price else None
    current_pe = safe_num(perdata.get("per")); p25=safe_num(perdata.get("pe_p25")); med=safe_num(perdata.get("pe_median")); p75=safe_num(perdata.get("pe_p75"))
    if current_pe is None or med is None:
        valuation_zone, valuation_risk = "資料不足", 0
    elif p75 is not None and current_pe >= p75:
        valuation_zone, valuation_risk = "高於歷史 P75", 85
    elif current_pe >= med:
        valuation_zone, valuation_risk = "高於歷史中位數", 60
    elif p25 is not None and current_pe <= p25:
        valuation_zone, valuation_risk = "低於歷史 P25", 20
    else:
        valuation_zone, valuation_risk = "歷史合理區間", 40

    rev_yoy = safe_num(revenue.get("revenue_yoy")); rev_3m = safe_num(revenue.get("revenue_3m_yoy"))
    growth_accel = (rev_yoy - rev_3m) if rev_yoy is not None and rev_3m is not None else None
    revision_score = 50
    if eps_rev is not None: revision_score += max(-25, min(25, eps_rev * 2.2))
    if target_rev is not None: revision_score += max(-20, min(20, target_rev * 1.5))
    revision_score += max(-15, min(15, event_tone_score * .15))
    if growth_accel is not None: revision_score += max(-10, min(10, growth_accel * .5))
    revision_score = round(max(0, min(100, revision_score)))

    fundamental_positive = (eps_rev is not None and eps_rev > 2) or (rev_yoy is not None and rev_yoy > 10) or event_tone_score > 20
    fundamental_negative = (eps_rev is not None and eps_rev < -2) or (rev_yoy is not None and rev_yoy < -10) or event_tone_score < -20
    if fundamental_positive and valuation_risk >= 80:
        regime = "基本面上修，但估值偏熱"
        summary = "獲利/營運預期偏上修，但目前估值已進入歷史高檔；後續股價更依賴 EPS 持續上修。"
    elif revision_score >= 65:
        regime = "市場預期上修"
        summary = "法人預估、目標價或公司事件訊號整體偏正向，市場預期正在改善。"
    elif revision_score <= 35 or fundamental_negative:
        regime = "市場預期下修"
        summary = "法人預估或公司營運訊號偏弱，市場預期存在下修風險。"
    else:
        regime = "預期中性／等待確認"
        summary = "目前上修與下修訊號未形成明確共識，宜等待下一輪法說、營收或法人修正。"

    signals = [
        {"name":"Forward EPS 修正", "value": eps_rev, "display": pct(eps_rev), "direction": "up" if eps_rev is not None and eps_rev>0 else "down" if eps_rev is not None and eps_rev<0 else "flat"},
        {"name":"法人目標價修正", "value": target_rev, "display": pct(target_rev), "direction": "up" if target_rev is not None and target_rev>0 else "down" if target_rev is not None and target_rev<0 else "flat"},
        {"name":"法人共識相對現價", "value": target_upside, "display": pct(target_upside), "direction": "up" if target_upside is not None and target_upside>0 else "down" if target_upside is not None and target_upside<0 else "flat"},
        {"name":"公司事件語氣", "value": event_tone_score, "display": f"{event_tone_score:+.0f}", "direction": "up" if event_tone_score>0 else "down" if event_tone_score<0 else "flat"},
        {"name":"估值位置", "value": valuation_risk, "display": valuation_zone, "direction": "risk" if valuation_risk>=60 else "flat"},
    ]
    return {
        "regime": regime, "summary": summary, "revision_score": revision_score, "valuation_risk": valuation_risk,
        "valuation_zone": valuation_zone, "event_tone_score": event_tone_score, "target_upside_pct": target_upside,
        "revenue_acceleration_pct": growth_accel, "signals": signals, "institution_revisions": revision_rows[:12],
        "methodology": "Forward EPS/目標價修正 + 公司事件語氣 + 營收動能 + 歷史 PER 位置；缺失欄位不補值。"
    }

def rsi(series: pd.Series, period: int = 14) -> float | None:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return safe_num(out.iloc[-1])


def macd(series: pd.Series) -> tuple[float | None, float | None, float | None]:
    if len(series) < 35:
        return None, None, None
    e12 = series.ewm(span=12, adjust=False).mean()
    e26 = series.ewm(span=26, adjust=False).mean()
    m = e12 - e26
    sig = m.ewm(span=9, adjust=False).mean()
    hist = m - sig
    return safe_num(m.iloc[-1]), safe_num(sig.iloc[-1]), safe_num(hist.iloc[-1])


def calc_technical(prices: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(prices)
    if df.empty or "close" not in df.columns:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close", "max", "min", "open", "Trading_Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["close"] > 0].sort_values("date")
    if df.empty:
        return {}
    s = df["close"]
    last = float(s.iloc[-1])
    ma = {k: (float(s.tail(k).mean()) if len(s) >= k else None) for k in (5, 10, 20, 60, 120, 240)}
    m, sig, hist = macd(s)
    r = rsi(s)
    high60 = float(s.tail(60).max()) if len(s) >= 20 else float(s.max())
    low60 = float(s.tail(60).min()) if len(s) >= 20 else float(s.min())
    support1 = ma[20] or low60
    support2 = ma[60] or low60
    trend = "多頭" if ma[20] and ma[60] and last > ma[20] > ma[60] else ("偏多" if ma[20] and last > ma[20] else "整理/偏弱")
    vol_ratio = None
    if "Trading_Volume" in df.columns and len(df) >= 21:
        v20 = df["Trading_Volume"].iloc[-21:-1].mean()
        vol_ratio = float(df["Trading_Volume"].iloc[-1] / v20) if v20 and not pd.isna(v20) else None
    returns20 = (last / float(s.iloc[-21]) - 1) * 100 if len(s) >= 21 else None
    returns60 = (last / float(s.iloc[-61]) - 1) * 100 if len(s) >= 61 else None
    return {
        "last": last, "last_date": df.iloc[-1]["date"].date().isoformat(), "ma": ma,
        "rsi14": r, "macd": m, "macd_signal": sig, "macd_hist": hist,
        "support1": support1, "support2": support2, "resistance": high60,
        "trend": trend, "volume_ratio_20": vol_ratio, "return_20d": returns20, "return_60d": returns60,
        "series": [{"date": d.date().isoformat(), "close": float(c)} for d, c in zip(df.tail(180)["date"], df.tail(180)["close"])],
        "high_52w": float(s.tail(252).max()), "low_52w": float(s.tail(252).min()),
    }


def calc_flow(rows: list[dict[str, Any]], margin_rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    result: dict[str, Any] = {}
    if not df.empty:
        for c in df.columns:
            if c not in ("date", "stock_id"):
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df = df.sort_values("date")
        def net(prefix: str, n: int) -> float:
            buy = [c for c in df.columns if c.startswith(prefix) and c.endswith("_buy")]
            sell = [c for c in df.columns if c.startswith(prefix) and c.endswith("_sell")]
            tail = df.tail(n)
            return float(tail[buy].sum().sum() - tail[sell].sum().sum()) if buy or sell else 0.0
        result.update({
            "foreign_5": net("Foreign_", 5), "foreign_20": net("Foreign_", 20),
            "trust_5": net("Investment_Trust", 5), "trust_20": net("Investment_Trust", 20),
            "dealer_20": net("Dealer", 20), "last_date": str(df.iloc[-1]["date"]),
        })
    mdf = pd.DataFrame(margin_rows)
    if not mdf.empty and "MarginPurchaseTodayBalance" in mdf.columns:
        mdf["MarginPurchaseTodayBalance"] = pd.to_numeric(mdf["MarginPurchaseTodayBalance"], errors="coerce")
        mdf = mdf.sort_values("date").dropna(subset=["MarginPurchaseTodayBalance"])
        if not mdf.empty:
            latest = float(mdf.iloc[-1]["MarginPurchaseTodayBalance"])
            prior = float(mdf.iloc[-21]["MarginPurchaseTodayBalance"]) if len(mdf) >= 21 else float(mdf.iloc[0]["MarginPurchaseTodayBalance"])
            result.update({"margin_balance": latest, "margin_20_pct": ((latest / prior) - 1) * 100 if prior else None, "margin_last_date": str(mdf.iloc[-1]["date"])})
    return result


def calc_revenue(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    for c in ["revenue", "revenue_year", "revenue_month"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["revenue"]).sort_values(["revenue_year", "revenue_month"])
    if df.empty:
        return {}
    last = df.iloc[-1]
    prev = df[(df["revenue_year"] == last["revenue_year"] - 1) & (df["revenue_month"] == last["revenue_month"])]
    yoy = None if prev.empty or float(prev.iloc[-1]["revenue"]) == 0 else (float(last["revenue"]) / float(prev.iloc[-1]["revenue"]) - 1) * 100
    last3 = df.tail(3)["revenue"].sum() if len(df) >= 3 else None
    prev3 = df.iloc[-15:-12]["revenue"].sum() if len(df) >= 15 else None
    yoy3 = (last3 / prev3 - 1) * 100 if last3 and prev3 else None
    return {
        "latest_revenue": float(last["revenue"]), "revenue_yoy": yoy, "revenue_3m_yoy": yoy3,
        "revenue_period": f"{int(last['revenue_year'])}-{int(last['revenue_month']):02d}", "last_date": str(last.get("date", "")),
        "series": [{"period": f"{int(r.revenue_year)}-{int(r.revenue_month):02d}", "revenue": float(r.revenue)} for r in df.tail(24).itertuples()],
    }


def calc_per(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df = df.sort_values("date")
    for c in ["PER", "PBR", "dividend_yield"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    r = df.iloc[-1]
    valid = df["PER"].dropna() if "PER" in df.columns else pd.Series(dtype=float)
    valid = valid[(valid > 0) & (valid < 200)]
    hist = {}
    if len(valid) >= 20:
        hist = {
            "pe_p25": float(valid.quantile(.25)), "pe_median": float(valid.quantile(.50)),
            "pe_p75": float(valid.quantile(.75)), "pe_mean": float(valid.mean()), "sample_count": int(len(valid)),
        }
    return {"per": safe_num(r.get("PER")), "pbr": safe_num(r.get("PBR")), "dividend_yield": safe_num(r.get("dividend_yield")), "last_date": str(r.get("date", "")), **hist}


def calc_financials(rows: list[dict[str, Any]]) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    dates = sorted(df["date"].unique())
    if not dates:
        return {}
    latest_date = dates[-1]
    q = df[df["date"] == latest_date]
    def pick(keys: list[str]) -> float | None:
        for k in keys:
            hit = q[q["type"].astype(str).str.lower().str.contains(k.lower(), regex=False)]
            if not hit.empty:
                return safe_num(hit.iloc[0]["value"])
        return None
    revenue = pick(["Revenue", "OperatingRevenue", "營業收入"])
    gross = pick(["GrossProfit", "營業毛利"])
    op = pick(["OperatingIncome", "營業利益"])
    net = pick(["NetIncome", "本期淨利", "ProfitLoss"])
    eps = pick(["BasicEarningsPerShare", "EarningsPerShare", "基本每股盈餘"])
    return {
        "statement_date": str(latest_date), "revenue": revenue, "gross_profit": gross, "operating_income": op, "net_income": net, "eps": eps,
        "gross_margin": (gross / revenue * 100) if gross is not None and revenue else None,
        "operating_margin": (op / revenue * 100) if op is not None and revenue else None,
        "net_margin": (net / revenue * 100) if net is not None and revenue else None,
    }


def load_research(ticker: str) -> list[dict[str, Any]]:
    p = DATA_DIR / "research_reports.json"
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in raw if str(x.get("ticker")) == ticker]
    except Exception:
        return []


def analyst_consensus(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda x: x.get("report_date", ""), reverse=True)
    targets = [safe_num(x.get("target_price")) for x in rows]
    targets = [x for x in targets if x is not None]
    epss = [safe_num(x.get("forward_eps")) for x in rows]
    epss = [x for x in epss if x is not None]
    revision = None
    dated_eps = [(x.get("report_date", ""), safe_num(x.get("forward_eps"))) for x in rows if safe_num(x.get("forward_eps")) is not None]
    if len(dated_eps) >= 2 and dated_eps[-1][1]:
        revision = (dated_eps[0][1] / dated_eps[-1][1] - 1) * 100
    return {"count": len(rows), "median_target": median(targets) if targets else None, "median_forward_eps": median(epss) if epss else None, "eps_revision_pct": revision, "reports": rows}


def model_valuation(price: float | None, perdata: dict[str, Any], eps_reported: float | None, consensus_eps: float | None) -> dict[str, Any]:
    if not price:
        return {"scenarios": [], "confidence": 0}
    if consensus_eps:
        anchor_eps = consensus_eps; eps_basis = "授權/匯入法人 Forward EPS 中位數"; eps_conf = 90
    elif eps_reported and eps_reported > 0:
        anchor_eps = eps_reported * 4; eps_basis = "最新單季 EPS × 4 年化（暫估，待 Forward EPS 取代）"; eps_conf = 55
    elif perdata.get("per") and perdata["per"] > 0:
        anchor_eps = price / perdata["per"]; eps_basis = "由現價 / 市場 PER 反推 TTM EPS"; eps_conf = 40
    else:
        return {"scenarios": [], "eps_basis": "資料不足", "confidence": 0}

    if perdata.get("pe_median"):
        bear_pe = max(5.0, perdata["pe_p25"])
        base_pe = perdata["pe_median"]
        bull_pe = min(150.0, perdata["pe_p75"])
        pe_basis = f"近年歷史 PER 分位數（樣本 {perdata.get('sample_count', 0)} 日）"
        pe_conf = 85
    else:
        center = perdata.get("per") if perdata.get("per") and 5 <= perdata["per"] <= 120 else 20.0
        bear_pe, base_pe, bull_pe = max(8.0, center * .8), center, min(150.0, center * 1.2)
        pe_basis = "目前 PER ±20%（歷史樣本不足時的降級模型）"
        pe_conf = 50
    scenarios = [
        {"name": "悲觀", "eps": anchor_eps * .90, "pe": bear_pe},
        {"name": "基準", "eps": anchor_eps, "pe": base_pe},
        {"name": "樂觀", "eps": anchor_eps * 1.10, "pe": bull_pe},
    ]
    for x in scenarios:
        x["target"] = x["eps"] * x["pe"]
        x["upside_pct"] = (x["target"] / price - 1) * 100
    return {"eps_basis": eps_basis, "pe_basis": pe_basis, "confidence": round((eps_conf + pe_conf) / 2), "scenarios": scenarios}


def scores(technical: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], perdata: dict[str, Any], financial: dict[str, Any], research: dict[str, Any]) -> dict[str, int]:
    fundamental = 50
    if revenue.get("revenue_yoy") is not None: fundamental += max(-20, min(25, revenue["revenue_yoy"] * .5))
    if revenue.get("revenue_3m_yoy") is not None: fundamental += max(-10, min(15, revenue["revenue_3m_yoy"] * .2))
    if financial.get("gross_margin") is not None: fundamental += 5 if financial["gross_margin"] > 30 else 0
    chip = 50 + (15 if flow.get("foreign_20", 0) > 0 else -15 if flow.get("foreign_20") is not None else 0) + (10 if flow.get("trust_20", 0) > 0 else -10 if flow.get("trust_20") is not None else 0)
    tech = 50 + (25 if technical.get("trend") == "多頭" else 12 if technical.get("trend") == "偏多" else -5)
    r = technical.get("rsi14")
    if r is not None: tech += 7 if 50 <= r <= 70 else (-8 if r > 80 or r < 25 else 0)
    if technical.get("macd_hist") is not None: tech += 5 if technical["macd_hist"] > 0 else -5
    valuation = 55
    pe = perdata.get("per")
    if pe and perdata.get("pe_median"):
        valuation += 15 if pe < perdata["pe_p25"] else (5 if pe <= perdata["pe_median"] else (-10 if pe > perdata["pe_p75"] else 0))
    elif pe: valuation += 8 if pe < 25 else (-8 if pe > 60 else 0)
    revision = 50
    if research.get("eps_revision_pct") is not None: revision += max(-25, min(30, research["eps_revision_pct"] * 2))
    d = {
        "基本面": round(max(0, min(100, fundamental))), "籌碼面": round(max(0, min(100, chip))),
        "技術面": round(max(0, min(100, tech))), "估值": round(max(0, min(100, valuation))),
        "預估修正": round(max(0, min(100, revision))),
    }
    d["綜合"] = round(d["基本面"]*.28 + d["籌碼面"]*.20 + d["技術面"]*.20 + d["估值"]*.17 + d["預估修正"]*.15)
    return d


def calc_confidence(source_status: list[dict[str, Any]], valuation: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    available = sum(1 for x in source_status if x["status"] == "ok")
    completeness = round(available / len(source_status) * 100) if source_status else 0
    research_bonus = 10 if research.get("count", 0) >= 3 else 5 if research.get("count", 0) else 0
    overall = round(completeness * .65 + valuation.get("confidence", 0) * .25 + research_bonus)
    return {"data_completeness": completeness, "valuation_confidence": valuation.get("confidence", 0), "research_coverage": research.get("count", 0), "overall": min(100, overall)}


def narrative(s: dict[str, int], tech: dict[str, Any], revenue: dict[str, Any], flow: dict[str, Any], valuation: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    base = next((x for x in valuation.get("scenarios", []) if x["name"] == "基準"), None)
    facts=[]
    if revenue.get("revenue_yoy") is not None: facts.append(f"最新月營收年增 {revenue['revenue_yoy']:+.1f}%")
    if revenue.get("revenue_3m_yoy") is not None: facts.append(f"近3月營收年增 {revenue['revenue_3m_yoy']:+.1f}%")
    if flow.get("foreign_20") is not None: facts.append(f"外資近20日淨買賣 {flow['foreign_20']:,.0f} 股")
    if tech.get("trend"): facts.append(f"技術趨勢 {tech['trend']}")
    if research.get("eps_revision_pct") is not None: facts.append(f"法人研究 Forward EPS 修正 {research['eps_revision_pct']:+.1f}%")
    if base: facts.append(f"模型基準合理價約 {base['target']:,.0f} 元")
    stance = "偏多" if s["綜合"] >= 75 else "中性偏多" if s["綜合"] >= 58 else "中性" if s["綜合"] >= 42 else "偏弱"
    catalysts=[]; risks=[]
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] > 15: catalysts.append("營收成長動能高於中性門檻")
    if flow.get("foreign_20",0) > 0 and flow.get("trust_20",0) > 0: catalysts.append("外資與投信近20日同向買超")
    if tech.get("trend") == "多頭": catalysts.append("中期均線結構維持多頭")
    if research.get("eps_revision_pct") is not None and research["eps_revision_pct"] > 3: catalysts.append("Forward EPS 共識出現上修")
    pe = valuation.get("scenarios", [{}])[1].get("pe") if len(valuation.get("scenarios", [])) > 1 else None
    if tech.get("rsi14") and tech["rsi14"] > 75: risks.append("RSI 偏高，短線追價風險上升")
    if flow.get("margin_20_pct") and flow["margin_20_pct"] > 10: risks.append("融資餘額快速增加，籌碼波動風險升高")
    if s["估值"] < 45: risks.append("目前估值相對自身歷史區間偏昂貴")
    if revenue.get("revenue_yoy") is not None and revenue["revenue_yoy"] < 0: risks.append("最新月營收仍呈年減")
    return {"stance": stance, "thesis": "；".join(facts)+"。" if facts else "目前公開結構化資料不足，系統不產生強結論。", "catalysts": catalysts[:4] or ["等待下一次營收、財報或法人預估出現明確上修訊號"], "risks": risks[:4] or ["模型假設與市場估值可能快速變動，需持續追蹤資料更新"]}


async def build_stock(ticker: str, force_refresh: bool = False) -> dict[str, Any]:
    if not force_refresh and ticker in _CACHE and time.time() - _CACHE[ticker][0] < CACHE_TTL:
        cached = dict(_CACHE[ticker][1]); cached["cache"] = {"hit": True, "ttl_seconds": CACHE_TTL}; return cached
    today = date.today(); errors: list[str] = []
    async def grab(dataset: str, days: int):
        try: return await finmind(dataset, ticker, today - timedelta(days=days), today)
        except Exception as e: errors.append(f"{dataset}: {type(e).__name__}"); return []
    async def info_grab():
        try:
            infos = await finmind("TaiwanStockInfo")
            return next((x for x in infos if str(x.get("stock_id")) == ticker), {})
        except Exception as e: errors.append(f"TaiwanStockInfo: {type(e).__name__}"); return {}

    info, prices, inst, margin, rev, pers, fin = await asyncio.gather(
        info_grab(), grab("TaiwanStockPrice", 460), grab("TaiwanStockInstitutionalInvestorsBuySellWide", 120),
        grab("TaiwanStockMarginPurchaseShortSale", 120), grab("TaiwanStockMonthRevenue", 900),
        grab("TaiwanStockPER", 1100), grab("TaiwanStockFinancialStatements", 1100)
    )
    tech=calc_technical(prices); flow=calc_flow(inst,margin); revenue=calc_revenue(rev); perdata=calc_per(pers); financial=calc_financials(fin)
    company_name=info.get("stock_name") or ticker
    try:
        web_research, company_events = await asyncio.gather(fetch_public_research(ticker, company_name), fetch_company_events(ticker, company_name))
    except Exception as e:
        errors.append(f"PublicWebResearch: {type(e).__name__}"); web_research={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}; company_events={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}
    research=merge_research(load_research(ticker), web_research.get("rows", []))
    lp=tech.get("last")
    valuation=model_valuation(lp,perdata,financial.get("eps"),research.get("median_forward_eps"))
    sc=scores(tech,revenue,flow,perdata,financial,research)
    nar=narrative(sc,tech,revenue,flow,valuation,research)
    expectation=expectation_gap_analysis(research, company_events, perdata, revenue, sc, lp)
    prev=tech.get("series",[])[-2]["close"] if len(tech.get("series",[]))>=2 else None
    change=((lp/prev-1)*100) if lp and prev else None
    source_status=[
        {"name":"股價","dataset":"TaiwanStockPrice","as_of":tech.get("last_date"),"status":"ok" if tech else "missing","scheduled_update":"交易日約 17:30"},
        {"name":"三大法人","dataset":"TaiwanStockInstitutionalInvestorsBuySellWide","as_of":flow.get("last_date"),"status":"ok" if flow.get("last_date") else "missing","scheduled_update":"交易日約 20:00"},
        {"name":"融資融券","dataset":"TaiwanStockMarginPurchaseShortSale","as_of":flow.get("margin_last_date"),"status":"ok" if flow.get("margin_last_date") else "missing","scheduled_update":"交易日約 21:00"},
        {"name":"月營收","dataset":"TaiwanStockMonthRevenue","as_of":revenue.get("last_date") or revenue.get("revenue_period"),"status":"ok" if revenue else "missing","scheduled_update":"依公司公告"},
        {"name":"PER/PBR","dataset":"TaiwanStockPER","as_of":perdata.get("last_date"),"status":"ok" if perdata else "missing","scheduled_update":"交易日約 18:00"},
        {"name":"財務報表","dataset":"TaiwanStockFinancialStatements","as_of":financial.get("statement_date"),"status":"ok" if financial else "missing","scheduled_update":"依財報公告"},
        {"name":"公開法人研究","dataset":"Google News RSS + 公開網路引用","as_of":web_research.get("fetched_at"),"status":"ok" if web_research.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
        {"name":"公司事件雷達","dataset":"公開新聞/法說/重大訊息引用","as_of":company_events.get("fetched_at"),"status":"ok" if company_events.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
    ]
    conf=calc_confidence(source_status,valuation,research)
    data={"ticker":ticker,"name":info.get("stock_name") or ticker,"industry":info.get("industry_category") or "—","market_type":info.get("type") or "—",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),"price":lp,"change_pct":change,"technical":tech,"revenue":revenue,"flow":flow,"per":perdata,"financial":financial,
          "research":research,"expectation_gap":expectation,"valuation":valuation,"scores":sc,"stance":nar["stance"],"thesis":nar["thesis"],"catalysts":nar["catalysts"],"risks":nar["risks"],"confidence":conf,
          "source_status":source_status,"errors":errors,"cache":{"hit":False,"ttl_seconds":CACHE_TTL},"web_research_meta":web_research,"company_events":company_events,
          "data_policy":"關鍵數值由結構化資料與公式計算；缺資料即標示缺失。法人區聚合公開網路標題、摘要、數值與來源連結，不重製付費全文；模型估值與市場法人共識分流。"}
    _CACHE[ticker]=(time.time(),data)
    return data


def report_html(d: dict[str, Any]) -> str:
    esc=lambda x: html.escape("—" if x is None else str(x))
    sc=d["scores"]; tech=d["technical"]; rev=d["revenue"]; flow=d["flow"]; per=d["per"]; fin=d["financial"]; research=d["research"]; exp=d.get("expectation_gap",{}); val=d["valuation"]; conf=d["confidence"]
    src="".join(f"<tr><td>{esc(x['name'])}</td><td>{esc(x.get('dataset'))}</td><td>{esc(x.get('as_of'))}</td><td>{esc(x['scheduled_update'])}</td><td>{'OK' if x['status']=='ok' else '缺資料'}</td></tr>" for x in d["source_status"])
    scenarios="".join(f"<tr><td>{x['name']}</td><td>{x['eps']:.2f}</td><td>{x['pe']:.1f}x</td><td><b>{x['target']:,.0f}</b></td><td>{x['upside_pct']:+.1f}%</td></tr>" for x in val.get("scenarios",[])) or "<tr><td colspan='5'>估值資料不足</td></tr>"
    rrows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('report_date'))}</td><td>{esc(x.get('rating'))}</td><td>{nfmt(safe_num(x.get('target_price')),0)}</td><td>{nfmt(safe_num(x.get('forward_eps')),2)}</td></tr>" for x in research.get("reports",[])) or "<tr><td colspan='5'>目前尚未搜尋到可解析的公開法人研究引用。</td></tr>"
    erows="".join(f"<tr><td>{esc(x.get('institution'))}</td><td>{esc(x.get('previous_date'))} → {esc(x.get('latest_date'))}</td><td>{pct(x.get('eps_revision_pct'))}</td><td>{pct(x.get('target_revision_pct'))}</td><td>{nfmt(x.get('latest_target'),0)}</td></tr>" for x in exp.get("institution_revisions",[])) or "<tr><td colspan='5'>同機構前後修正資料不足。</td></tr>"
    cats="".join(f"<li>{esc(x)}</li>" for x in d.get("catalysts",[])); risks="".join(f"<li>{esc(x)}</li>" for x in d.get("risks",[]))
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><style>
    @page{{size:A4;margin:11mm}} body{{font-family:'Noto Sans TC','PingFang TC',sans-serif;color:#13202a;font-size:9.5pt;line-height:1.55}} h1{{font-size:23pt;margin:0}} h2{{font-size:14pt;border-bottom:2px solid #173847;padding-bottom:4px;margin:18px 0 8px}} .muted{{color:#60727c}} .head{{display:flex;justify-content:space-between;border-bottom:3px solid #173847;padding-bottom:9px}} .price{{font-size:23pt;font-weight:800;text-align:right}} .pill{{display:inline-block;border:1px solid #719188;border-radius:20px;padding:2px 8px;margin-right:5px}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0}} .card{{border:1px solid #d7e0e4;border-radius:7px;padding:8px}} .card b{{font-size:14pt;display:block}} table{{width:100%;border-collapse:collapse;font-size:8.4pt}} th,td{{padding:5px;border-bottom:1px solid #dce4e8;text-align:left}} th{{background:#f2f6f7}} .call{{border-left:4px solid #17866b;background:#f4faf8;padding:9px}} .warn{{border:1px solid #d7b94b;background:#fff9e7;padding:8px;margin-top:10px}} .small{{font-size:8pt}} .page-break{{break-before:page}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:10px}} ul{{margin:4px 0 0;padding-left:18px}} .badge{{font-weight:700}}
    </style></head><body>
    <div class='head'><div><div class='muted'>AI STOCK RESEARCH TERMINAL V5.1 EXPECTATION GAP • TAIWAN EQUITY RESEARCH</div><h1>{esc(d['name'])} <span class='muted'>{esc(d['ticker'])}</span></h1><div><span class='pill'>{esc(d['industry'])}</span><span class='pill'>{esc(d['stance'])}</span><span class='pill'>可信度 {conf['overall']}/100</span></div></div><div><div class='muted'>最新收盤</div><div class='price'>{nfmt(d['price'],1)}</div><div>{pct(d['change_pct'])}</div></div></div>
    <div class='small muted'>報告產生：{esc(d['generated_at'])} ｜ 資料完整度：{conf['data_completeness']}% ｜ 估值信心：{conf['valuation_confidence']}%</div>
    <h2>1. Executive Summary</h2><div class='call'>{esc(d['thesis'])}</div>
    <div class='grid'><div class='card'>綜合評分<b>{sc['綜合']}/100</b></div><div class='card'>基本面<b>{sc['基本面']}</b></div><div class='card'>籌碼面<b>{sc['籌碼面']}</b></div><div class='card'>技術面<b>{sc['技術面']}</b></div></div>
    <div class='cols'><div><b>主要催化劑</b><ul>{cats}</ul></div><div><b>主要風險</b><ul>{risks}</ul></div></div>
    <h2>2. Expectation Gap & Revision Radar</h2><div class='call'><b>{esc(exp.get('regime'))}</b><br>{esc(exp.get('summary'))}</div><div class='grid'><div class='card'>預期修正分數<b>{exp.get('revision_score','—')}/100</b></div><div class='card'>EPS 修正<b>{pct(research.get('eps_revision_pct'))}</b></div><div class='card'>目標價修正<b>{pct(research.get('target_revision_pct'))}</b></div><div class='card'>估值區域<b style='font-size:10pt'>{esc(exp.get('valuation_zone'))}</b></div></div><table><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr>{erows}</table><p class='small muted'>{esc(exp.get('methodology'))}</p>
    <h2>3. Fundamentals</h2><table><tr><th>最新月營收</th><th>YoY</th><th>3M YoY</th><th>最新 EPS</th><th>毛利率</th><th>營益率</th></tr><tr><td>{nfmt(rev.get('latest_revenue'),0)}</td><td>{pct(rev.get('revenue_yoy'))}</td><td>{pct(rev.get('revenue_3m_yoy'))}</td><td>{nfmt(fin.get('eps'),2)}</td><td>{pct(fin.get('gross_margin'))}</td><td>{pct(fin.get('operating_margin'))}</td></tr></table>
    <h2>4. Positioning & Technicals</h2><table><tr><th>外資20日</th><th>投信20日</th><th>融資20日</th><th>趨勢</th><th>RSI14</th><th>量比</th><th>支撐 / 壓力</th></tr><tr><td>{nfmt(flow.get('foreign_20'),0)}</td><td>{nfmt(flow.get('trust_20'),0)}</td><td>{pct(flow.get('margin_20_pct'))}</td><td>{esc(tech.get('trend'))}</td><td>{nfmt(tech.get('rsi14'),1)}</td><td>{nfmt(tech.get('volume_ratio_20'),2)}x</td><td>{nfmt(tech.get('support1'),1)} / {nfmt(tech.get('resistance'),1)}</td></tr></table>
    <div class='page-break'></div><h2>5. Analyst Research & Revisions</h2><p>匯入報告數：<b>{research.get('count',0)}</b> ｜ Forward EPS 修正：<b>{pct(research.get('eps_revision_pct'))}</b></p><table><tr><th>法人/券商</th><th>日期</th><th>評等</th><th>目標價</th><th>Forward EPS</th></tr>{rrows}</table><p class='small muted'>本區彙整公開網路可取得之研究引用與使用者匯入資料；僅保存標題、摘要、數值、發布者與來源連結，不重製付費研究全文。</p>
    <h2>6. Company Events & Earnings-call Radar</h2><table><tr><th>日期</th><th>事件</th><th>發布者</th></tr>{''.join(f"<tr><td>{esc(x.get('date'))}</td><td>{esc(x.get('title'))}</td><td>{esc(x.get('publisher'))}</td></tr>" for x in d.get('company_events',{{}}).get('rows',[])[:8]) or "<tr><td colspan='3'>目前未搜尋到公司事件引用。</td></tr>"}</table>
    <h2>7. Valuation Framework</h2><p>EPS：{esc(val.get('eps_basis'))}<br>PE：{esc(val.get('pe_basis'))}</p><table><tr><th>情境</th><th>EPS</th><th>合理 PE</th><th>模型合理價</th><th>相對現價</th></tr>{scenarios}</table>
    <p class='small muted'>歷史 PER：P25 {nfmt(per.get('pe_p25'),1)}x / Median {nfmt(per.get('pe_median'),1)}x / P75 {nfmt(per.get('pe_p75'),1)}x；模型合理價與法人目標價分開呈現。</p>
    <h2>8. Data Lineage & Freshness</h2><table><tr><th>資料</th><th>Dataset</th><th>截至</th><th>預定更新</th><th>狀態</th></tr>{src}</table>
    <div class='warn'><b>重要揭露</b><br>本報告為研究與資訊整理工具，不構成個人化投資建議、招攬或收益保證。模型估值對 EPS 與估值倍數高度敏感；請以每列資料截至日與來源為準。</div>
    </body></html>"""


@app.get("/")
async def home(): return FileResponse(ROOT / "index.html")
@app.get("/app.js")
async def js(): return FileResponse(ROOT / "app.js", media_type="application/javascript", headers={"Cache-Control":"no-cache"})
@app.get("/styles.css")
async def css(): return FileResponse(ROOT / "styles.css", media_type="text/css", headers={"Cache-Control":"no-cache"})
@app.get("/sw.js")
async def sw(): return FileResponse(ROOT / "sw.js", media_type="application/javascript", headers={"Cache-Control":"no-cache", "Service-Worker-Allowed":"/"})

@app.get("/api/stock/{ticker}")
async def stock_api(ticker: str, refresh: bool = Query(False)):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6): raise HTTPException(400,"請輸入有效台股代號")
    d=await build_stock(ticker, force_refresh=refresh)
    if d["price"] is None and d["name"] == ticker: raise HTTPException(404,"查無股票或資料來源暫時無法連線")
    return JSONResponse(d, headers={"Cache-Control":"no-store"})

@app.get("/api/stock/{ticker}/pdf")
async def stock_pdf(ticker: str, refresh: bool = Query(True)):
    d=await build_stock(ticker.strip(), force_refresh=refresh)
    if d["price"] is None: raise HTTPException(503,"目前無法取得股價資料，為避免輸出錯誤報告，PDF 未產生。")
    stamp=datetime.now().strftime("%Y%m%d_%H%M")
    out=REPORT_DIR/f"{ticker}_{stamp}_research_v5_1.pdf"
    HTML(string=report_html(d),base_url=str(ROOT)).write_pdf(out)
    return FileResponse(out,media_type="application/pdf",filename=f"{ticker}_AI_research_V5_1_{stamp}.pdf")

@app.post("/api/cache/clear")
async def cache_clear():
    _CACHE.clear(); return {"status":"ok","message":"cache cleared"}

@app.get("/health")
async def health(): return {"status":"ok","version":"5.1.2","mode":"cloud-mobile-expectation-gap","finmind_token":bool(FINMIND_TOKEN),"cache_ttl_seconds":CACHE_TTL,"pwa":True}
