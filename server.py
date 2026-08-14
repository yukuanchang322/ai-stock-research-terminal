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
import csv
import io
from urllib.parse import urljoin
from email.utils import parsedate_to_datetime

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from weasyprint import HTML
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "generated_reports"
DATA_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

app = FastAPI(title="AI Stock Research Terminal", version="5.2.7")
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


TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"

def parse_num_text(v: Any) -> float | None:
    if v is None: return None
    s=str(v).strip().replace(",", "").replace("％", "").replace("%", "")
    if s in ("", "-", "--", "—", "－"): return None
    try: return float(s)
    except Exception: return None

def roc_date_to_iso(v: Any) -> str:
    s=re.sub(r"[^0-9]", "", str(v or ""))
    try:
        if len(s)==7: return f"{int(s[:3])+1911:04d}-{s[3:5]}-{s[5:7]}"
        if len(s)==8: return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except Exception: pass
    return str(v or "")

def _row_value(row: dict[str, Any], includes: list[str], excludes: list[str] | None=None) -> Any:
    excludes=excludes or []
    for k,v in row.items():
        ks=str(k).replace(" ", "")
        if all(x in ks for x in includes) and not any(x in ks for x in excludes): return v
    return None

async def openapi_json(base: str, path: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7"}) as client:
        r=await client.get(base + path); r.raise_for_status(); data=r.json()
    return data if isinstance(data,list) else []



MOPS_CSV_BASE = "https://mopsfin.twse.com.tw/opendata"
IR_FINANCIAL_PAGES = {
    "3661": "https://www.alchip.com/en/Investors/financials/report",
}

async def mops_csv_rows(filename: str) -> list[dict[str, Any]]:
    """Official MOPS CSV fallback. Some foreign/KY issuers can appear here even when JSON feeds lag."""
    url=f"{MOPS_CSV_BASE}/{filename}"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7"}) as client:
        r=await client.get(url); r.raise_for_status()
    raw=r.content
    text=None
    for enc in ("utf-8-sig","utf-8","cp950","big5"):
        try:
            text=raw.decode(enc); break
        except Exception:
            continue
    if text is None: return []
    return [dict(x) for x in csv.DictReader(io.StringIO(text))]

def _official_row_to_snapshot(row: dict[str, Any], source: str, endpoint: str, market: str="上市", kind: str="summary") -> dict[str, Any] | None:
    def val(*names):
        for name in names:
            if name in row and row.get(name) not in (None,""): return row.get(name)
        return None
    code=val("公司代號","SecuritiesCompanyCode","公司代碼","代號")
    if code is None:
        code=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
    eps=parse_num_text(val("基本每股盈餘(元)","基本每股盈餘","每股盈餘") or _row_value(row,["基本每股盈餘"]) or _row_value(row,["每股盈餘"]))
    revenue=parse_num_text(val("營業收入") or _row_value(row,["營業收入"],["百分比"]))
    gross=parse_num_text(val("營業毛利（毛損）","營業毛利(毛損)") or _row_value(row,["營業毛利"],["百分比"]))
    op=parse_num_text(val("營業利益（損失）","營業利益(損失)","營業利益") or _row_value(row,["營業利益"],["百分比"]))
    net=parse_num_text(val("本期淨利（淨損）","本期淨利(淨損)","本期淨利","稅後淨利") or _row_value(row,["本期淨利"],["百分比"]) or _row_value(row,["稅後淨利"]))
    year=val("年度") or _row_value(row,["年度"]) or _row_value(row,["年"])
    quarter=val("季別") or _row_value(row,["季別"]) or _row_value(row,["季"])
    out_date=val("出表日期","資料日期") or _row_value(row,["出表日期"]) or _row_value(row,["資料日期"])
    try:
        y=int(str(year).strip()); y=y+1911 if y<1911 else y
    except Exception: y=None
    q=None; qm=re.search(r"([1-4])",str(quarter or "")); q=int(qm.group(1)) if qm else None
    period=f"{y} Q{q}" if y and q else (roc_date_to_iso(out_date) or "latest")
    return {"source":source,"market":market,"endpoint":endpoint,"official":True,"period":period,
            "fiscal_year":y,"fiscal_quarter":q,"statement_date":roc_date_to_iso(out_date),
            "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
            "operating_income_ytd":op,"net_income_ytd":net,"raw_keys":list(row.keys())[:30],
            "feed_kind":kind,"company_code":re.sub(r"\D","",str(code or "")) or str(code or "").strip(),
            "completeness":sum(v is not None for v in (eps,revenue,gross,op,net))}

async def fetch_mops_csv_official(ticker: str) -> list[dict[str, Any]]:
    files=[
        ("t187ap14_L.csv","TWSE/MOPS EPS CSV","summary"),
        ("t187ap06_L_ci.csv","TWSE/MOPS Income CSV","detail"),
        ("t187ap06_L_mim.csv","TWSE/MOPS Income CSV","detail"),
        ("t187ap06_X_ci.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
        ("t187ap06_X_mim.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
    ]
    out=[]
    for filename,source,kind in files:
        try:
            rows=await mops_csv_rows(filename)
            for row in rows:
                snap=_official_row_to_snapshot(row,source,filename,"上市/公發",kind)
                if snap and snap.get("company_code")==ticker:
                    out.append(snap); break
        except Exception:
            continue
    return out

def _extract_pdf_financials(text: str, year: int, quarter: int) -> dict[str, Any]:
    """Parse reviewed/audited IR PDFs conservatively.

    For Q2/Q3 Alchip-style statements the columns are:
    current quarter, prior-year quarter, current YTD, prior-year YTD.
    Therefore the third amount/EPS is the current YTD value. We only accept
    explicit table patterns; ambiguity returns None instead of guessing.
    """
    compact=" ".join(text.split())
    def num(x): return parse_num_text(str(x).replace("$", "").replace("(", "-").replace(")", ""))
    def eps_current_ytd():
        if quarter in (2,3):
            m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)\s+\$?\s*([0-9.]+)", compact, re.I)
            return num(m.group(3)) if m else None
        if quarter==1:
            m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*[0-9.]+\s+\$?\s*([0-9.]+)",compact,re.I)
            return num(m.group(1)) if m else None
        m=re.search(r"(?:EARNINGS PER SHARE.*?)(?:Basic)\s+\$?\s*[0-9.]+\s+\$?\s*([0-9.]+)",compact,re.I)
        return num(m.group(1)) if m else None
    def table_ytd(label: str, pct_pattern: str="(?:100|[0-9]{1,2})"):
        # Amount/% repeated four times; choose current YTD (third amount) for Q2/Q3.
        pat=rf"{label}.*?\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}\s+\$?\s*([0-9,]+)\s+{pct_pattern}"
        m=re.search(pat,compact,re.I)
        if m: return num(m.group(3))
        # Q1 / annual two-column form: USD and NTD (or current/prior year); prefer NTD second amount.
        m=re.search(rf"{label}.*?\$?\s*([0-9,]+)\s+\$?\s*([0-9,]+)",compact,re.I)
        return num(m.group(2)) if m else None
    eps=eps_current_ytd()
    revenue=table_ytd(r"OPERATING REVENUE(?:\s*\(Note\s*20\))?", "100")
    gross=table_ytd(r"GROSS PROFIT")
    op=table_ytd(r"PROFIT FROM OPERATIONS")
    net=table_ytd(r"NET PROFIT FOR THE PERIOD")
    return {"ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,"operating_income_ytd":op,"net_income_ytd":net}

async def fetch_company_ir_financial(ticker: str, expected_year: int, expected_quarter: int) -> dict[str, Any] | None:
    page=IR_FINANCIAL_PAGES.get(ticker)
    if not page: return None
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7"}) as client:
            r=await client.get(page); r.raise_for_status(); page_html=r.text
            hrefs=re.findall(r'href=["\']([^"\']+)["\']',page_html,re.I)
            embedded=re.findall(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',page_html,re.I)
            # Prefer PDFs whose URL/text suggests the expected year/quarter. Hidden/data-attribute PDF URLs are included too.
            pdfs=[]
            for href in list(dict.fromkeys(hrefs+embedded)):
                if ".pdf" not in href.lower(): continue
                url=urljoin(str(r.url),href)
                score=0
                lo=url.lower()
                if str(expected_year) in lo: score+=4
                if f"q{expected_quarter}" in lo or f"_{expected_quarter}_" in lo: score+=5
                pdfs.append((score,url))
            pdfs=sorted(set(pdfs),reverse=True)[:12]
            for _,url in pdfs:
                pr=await client.get(url); pr.raise_for_status()
                reader=PdfReader(io.BytesIO(pr.content))
                text="\n".join((page.extract_text() or "") for page in reader.pages[:12])
                # Period must be explicit; never infer a current quarter from download date.
                low=text.lower()
                if expected_quarter==2:
                    period_ok=(str(expected_year) in text and ("six months ended june 30" in low or "六月三十日" in text or "6月30日" in text))
                elif expected_quarter==1:
                    period_ok=(str(expected_year) in text and ("three months ended march 31" in low or "三月三十一日" in text or "3月31日" in text))
                elif expected_quarter==3:
                    period_ok=(str(expected_year) in text and ("nine months ended september 30" in low or "九月三十日" in text or "9月30日" in text))
                else:
                    period_ok=(str(expected_year) in text and ("year ended december 31" in low or "十二月三十一日" in text or "12月31日" in text))
                if not period_ok: continue
                vals=_extract_pdf_financials(text,expected_year,expected_quarter)
                return {"source":"Company IR audited/reviewed PDF","market":"IR","endpoint":url,"official":True,
                        "period":f"{expected_year} Q{expected_quarter}","fiscal_year":expected_year,"fiscal_quarter":expected_quarter,
                        "statement_date":None,"feed_kind":"ir_pdf","completeness":sum(v is not None for v in vals.values()),
                        **vals,"ir_page":page}
    except Exception:
        return None
    return None


def _text_field(row: dict[str, Any], *names: str) -> str:
    """Read a MOPS field while tolerating invisible/trailing spaces in official keys."""
    wanted={re.sub(r"\s+","",x) for x in names}
    for k,v in row.items():
        if re.sub(r"\s+","",str(k)) in wanted:
            return str(v or "")
    return ""


def _roc_year_to_ad(v: str | int | None) -> int | None:
    if v is None: return None
    m=re.search(r"(\d{2,4})",str(v))
    if not m: return None
    y=int(m.group(1))
    return y+1911 if y < 1911 else y


def _announcement_period(text: str) -> tuple[int|None,int|None]:
    """Resolve fiscal year/quarter from MOPS board-approved financial-report disclosures."""
    compact=re.sub(r"\s+","",text)
    # Highest-confidence: explicit report start/end date, e.g. 115/01/01~115/06/30.
    m=re.search(r"(?:報導期間|報告期間|起訖日期).*?(\d{2,4})[/-]0?1[/-]0?1.*?(\d{2,4})[/-](0?3|0?6|0?9|12)[/-](?:30|31)",compact,re.I)
    if m:
        y=_roc_year_to_ad(m.group(2) or m.group(1)); month=int(m.group(3)); q={3:1,6:2,9:3,12:4}.get(month)
        if y and q: return y,q
    # Fallback: 115年度第二季 / 115年第2季.
    m=re.search(r"(\d{2,4})年(?:度)?第?([一二三四1234])季",compact)
    if m:
        qmap={"一":1,"二":2,"三":3,"四":4,"1":1,"2":2,"3":3,"4":4}
        return _roc_year_to_ad(m.group(1)),qmap.get(m.group(2))
    return None,None


def _extract_mops_financial_announcement(row: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    """Parse the official MOPS material-information disclosure for board-approved quarterly financials.

    This is intentionally conservative: it only accepts announcements whose subject/description explicitly
    say the board approved a quarterly/annual financial report and whose report period can be resolved.
    """
    code=_text_field(row,"公司代號","公司代碼")
    if re.sub(r"\D","",code) != ticker: return None
    subject=_text_field(row,"主旨","主旨 ")
    desc=_text_field(row,"說明")
    blob=f"{subject}\n{desc}"
    key=re.sub(r"\s+","",blob)
    if not ("財務報告" in key and ("董事會通過" in key or "董事會決議" in key or "提報董事會" in key)):
        return None
    fy,fq=_announcement_period(blob)
    if not fy or not fq: return None
    def pick(patterns):
        for pat in patterns:
            m=re.search(pat,blob,re.I|re.S)
            if m:
                return parse_num_text(m.group(1))
        return None
    # MOPS amounts here are explicitly stated in NT$ thousand; keep the same unit as official income statements.
    revenue=pick([r"累計至本期止營業收入[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業收入\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    gross=pick([r"累計至本期止營業毛利[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業毛利[^:：]*\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    op=pick([r"累計至本期止營業利益[^:：]*[:：]\s*([\-0-9,\.]+)",r"營業利益[^:：]*\(仟元\)[^:：]*[:：]\s*([\-0-9,\.]+)"])
    net=pick([r"歸屬於母公司業主淨利[^:：]*[:：]\s*([\-0-9,\.]+)",r"累計至本期止本期淨利[^:：]*[:：]\s*([\-0-9,\.]+)"])
    eps=pick([r"基本每股盈餘(?:\(損失\))?[^:：]*[:：]\s*([\-0-9,\.]+)",r"每股盈餘[^:：]*[:：]\s*([\-0-9,\.]+)"])
    speech=roc_date_to_iso(_text_field(row,"發言日期") or _text_field(row,"出表日期"))
    return {
        "source":"TWSE/MOPS board-approved financial disclosure",
        "market":"上市重大訊息","endpoint":"/opendata/t187ap04_L","official":True,
        "period":f"{fy} Q{fq}","fiscal_year":fy,"fiscal_quarter":fq,"statement_date":speech,
        "feed_kind":"material_announcement","company_code":ticker,"ytd_eps":eps,
        "revenue_ytd":revenue,"gross_profit_ytd":gross,"operating_income_ytd":op,"net_income_ytd":net,
        "completeness":sum(v is not None for v in (eps,revenue,gross,op,net)),
        "subject":subject,"announcement_date":speech,
    }


async def fetch_mops_material_financial(ticker: str, expected_year: int | None=None, expected_quarter: int | None=None) -> list[dict[str, Any]]:
    """Official fallback for foreign/KY issuers whose XBRL summary feed lags.

    Layer A uses TWSE's daily MOPS material-information OpenAPI. Layer B tries the official MOPS
    historical-search endpoint so a disclosure from one or two days ago is still discoverable.
    """
    out=[]
    ua={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7"}
    async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=ua) as client:
        # A. Daily official material-information feed.
        try:
            r=await client.get(TWSE_OPENAPI+"/opendata/t187ap04_L"); r.raise_for_status(); data=r.json()
            for row in (data if isinstance(data,list) else []):
                snap=_extract_mops_financial_announcement(row,ticker)
                if snap: out.append(snap)
        except Exception:
            pass
        # B. Official MOPS historical material-information search. The HTML response contains the same
        # disclosure text; this layer is best-effort because MOPS occasionally changes presentation HTML.
        try:
            roc_year=(expected_year-1911) if expected_year else (date.today().year-1911)
            form={"encodeURIComponent":"1","step":"1","firstin":"1","off":"1","TYPEK":"all",
                  "co_id":ticker,"year":str(roc_year),"month":f"{date.today().month:02d}"}
            r=await client.post("https://mops.twse.com.tw/mops/web/ajax_t05st01",data=form)
            if r.status_code==200 and ticker in r.text:
                text=html.unescape(re.sub(r"<[^>]+>","\n",r.text))
                text=re.sub(r"[\t\r ]+"," ",text)
                # Locate report-approval blocks and convert to pseudo rows for the shared parser.
                for m in re.finditer(r"(.{0,240}(?:董事會通過|董事會決議).{0,1200}財務報告.{0,2400})",text,re.S):
                    block=m.group(1)
                    snap=_extract_mops_financial_announcement({"公司代號":ticker,"主旨":"董事會通過財務報告","說明":block,"發言日期":""},ticker)
                    if snap: out.append(snap)
        except Exception:
            pass
    # Deduplicate by period, keep most complete.
    best={}
    for x in out:
        k=(x.get("fiscal_year"),x.get("fiscal_quarter"))
        if k not in best or x.get("completeness",0)>best[k].get("completeness",0): best[k]=x
    return list(best.values())




def _decode_mops_html(raw: bytes, apparent: str | None = None) -> str:
    """Decode MOPS legacy/server-java pages. The endpoint may still emit Big5/CP950."""
    encs=[]
    if apparent: encs.append(apparent)
    encs += ["utf-8-sig","utf-8","cp950","big5"]
    for enc in encs:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _flatten_cols(cols) -> list[str]:
    out=[]
    for c in cols:
        if isinstance(c, tuple):
            parts=[str(x).strip() for x in c if str(x).strip() and not str(x).startswith("Unnamed")]
            out.append(" ".join(parts))
        else:
            out.append(str(c).strip())
    return out


def _norm_label(v: Any) -> str:
    return re.sub(r"[\s　:：()（）/／\\\-]", "", str(v or "")).lower()


def _pick_income_row(df: pd.DataFrame, aliases: list[str]) -> int | None:
    if df.empty: return None
    # Account name is normally one of the first 2 columns; scan all cells for resilience.
    aliases_n=[_norm_label(x) for x in aliases]
    for i,row in df.iterrows():
        for v in row.iloc[:3].tolist():
            t=_norm_label(v)
            if any(a and a in t for a in aliases_n):
                return i
    return None


def _target_ytd_column(df: pd.DataFrame, quarter: int) -> Any | None:
    """Resolve current-period cumulative/YTD amount column in MOPS IFRS tables."""
    cols=_flatten_cols(df.columns)
    # Strongest signals first. MultiIndex from read_html usually preserves these labels.
    keys=["本期累計","本年度截至本季止","本年度截至本期止","本期累計數","本期累計金額","累計"]
    for key in keys:
        hits=[i for i,c in enumerate(cols) if key in c and not any(x in c for x in ("去年","前期","上期"))]
        if hits:
            # Prefer amount rather than percentage.
            for i in hits:
                if not any(x in cols[i] for x in ("%","％","百分比")): return df.columns[i]
            return df.columns[hits[0]]
    # Q1 is both quarter and YTD. Prefer first non-percentage numeric column after account label.
    if quarter==1:
        for i,c in enumerate(cols[1:], start=1):
            if not any(x in c for x in ("%","％","百分比")): return df.columns[i]
    # Legacy tables can expose four amount columns without useful flattened headers:
    # current quarter, prior-year quarter, current YTD, prior-year YTD.
    candidates=[df.columns[i] for i,c in enumerate(cols[1:], start=1) if not any(x in c for x in ("%","％","百分比"))]
    if quarter in (2,3) and len(candidates)>=3: return candidates[2]
    if candidates: return candidates[0]
    return None


def _extract_mops_ifrs_tables(html_text: str, ticker: str, year: int, quarter: int, report_id: str, url: str) -> dict[str, Any] | None:
    """Parse a company-specific MOPS IFRS report page into a same-period snapshot."""
    if "查無資料" in html_text or "無符合條件" in html_text: return None
    try:
        tables=pd.read_html(io.StringIO(html_text))
    except Exception:
        return None
    income=None
    for df in tables:
        flat=" ".join(str(x) for x in df.astype(str).fillna("").values.ravel()[:800])
        if ("每股盈餘" in flat or "Earnings per share" in flat) and ("營業收入" in flat or "Operating revenue" in flat):
            income=df.copy(); break
    if income is None: return None
    col=_target_ytd_column(income, quarter)
    if col is None: return None
    def value(aliases):
        idx=_pick_income_row(income, aliases)
        if idx is None: return None
        try:
            return parse_num_text(income.loc[idx, col])
        except Exception:
            return None
    eps=value(["基本每股盈餘合計","基本每股盈餘","每股盈餘","Basic earnings per share"])
    revenue=value(["營業收入合計","營業收入","Operating revenue","Revenue"])
    gross=value(["營業毛利（毛損）淨額","營業毛利（毛損）","營業毛利","Gross profit"])
    op=value(["營業利益（損失）","營業利益","Profit from operations","Operating income"])
    net=value(["本期淨利（淨損）","本期淨利","歸屬於母公司業主（淨利∕損）","歸屬於母公司業主之淨利","Net income"])
    completeness=sum(v is not None for v in (eps,revenue,gross,op,net))
    if completeness==0: return None
    return {
        "source":"MOPS company IFRS report","market":"MOPS","endpoint":url,"official":True,
        "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,
        "statement_date":None,"feed_kind":f"mops_company_{report_id}","company_code":ticker,
        "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
        "operating_income_ytd":op,"net_income_ytd":net,"completeness":completeness,
        "report_id":report_id,"table_count":len(tables),"selected_column":str(col),
    }


async def fetch_mops_company_ifrs(ticker: str, year: int, quarter: int) -> list[dict[str, Any]]:
    """Company-specific official MOPS report, avoiding incomplete aggregate feeds.

    REPORT_ID C=consolidated, B=individual/standalone, A=individual legacy. We try all and
    retain valid same-period results; downstream selection chooses the most complete row.
    """
    base="https://mops.twse.com.tw/server-java/t164sb01"
    out=[]
    headers={"User-Agent":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
             "Referer":"https://mops.twse.com.tw/mops/"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        for report_id in ("C","B","A"):
            params={"step":"1","CO_ID":ticker,"SYEAR":str(year),"SSEASON":str(quarter),"REPORT_ID":report_id}
            try:
                r=await client.get(base,params=params)
                if r.status_code!=200 or len(r.content)<500: continue
                text=_decode_mops_html(r.content, r.encoding)
                snap=_extract_mops_ifrs_tables(text,ticker,year,quarter,report_id,str(r.url))
                if snap: out.append(snap)
            except Exception:
                continue
    return out


async def fetch_tsmc_quarterly_release(year: int, quarter: int) -> dict[str, Any] | None:
    """TSMC official IR fallback. Gives a verified quarter even before MOPS aggregate refresh."""
    if quarter not in (1,2,3,4): return None
    url=f"https://investor.tsmc.com/english/quarterly-results/{year}/q{quarter}"
    headers={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7"}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            r=await client.get(url); r.raise_for_status(); text=html.unescape(re.sub(r"<[^>]+>"," ",r.text))
        compact=" ".join(text.split())
        # Quarterly results page reliably publishes actual gross and operating margins.
        gm=None; om=None
        mg=re.search(r"Gross Margin\s+([0-9.]+)%",compact,re.I); gm=parse_num_text(mg.group(1)) if mg else None
        mo=re.search(r"Operating Margin\s+([0-9.]+)%",compact,re.I); om=parse_num_text(mo.group(1)) if mo else None
        # Try to catch EPS if the earnings release text is embedded/linked into the rendered page.
        me=re.search(r"EPS(?:\s+of)?\s+NT\$\s*([0-9.]+)",compact,re.I); qeps=parse_num_text(me.group(1)) if me else None
        if gm is None and om is None and qeps is None: return None
        return {"source":"TSMC official quarterly results","market":"Company IR","endpoint":url,"official":True,
                "period":f"{year} Q{quarter}","fiscal_year":year,"fiscal_quarter":quarter,"statement_date":None,
                "feed_kind":"company_ir_quarter","company_code":"2330","quarter_eps_direct":qeps,
                "gross_margin_direct":gm,"operating_margin_direct":om,"completeness":sum(v is not None for v in (qeps,gm,om))}
    except Exception:
        return None

async def fetch_official_income_statement(ticker: str) -> dict[str, Any]:
    """Fetch the newest official MOPS income-statement row across listed/OTC industry schemas.

    Do not stop at the first endpoint: a company can live in a schema other than general industry,
    and different endpoints may refresh at slightly different times. The newest fiscal period wins.
    """
    industry_suffixes=["ci","mim","basi","bd","fh","ins"]
    candidates=[]
    # Daily EPS summary is the fastest official period anchor and is not tied to an industry schema.
    # It is especially useful for KY/foreign issuers whose detailed statement may live outside the usual L_* feed.
    candidates.append((TWSE_OPENAPI, "/opendata/t187ap14_L", "TWSE/MOPS EPS Daily Summary", "上市", "summary"))
    candidates.append((TPEX_OPENAPI, "/mopsfin_t187ap14_O", "TPEx/MOPS EPS Daily Summary", "上櫃", "summary"))
    for suffix in industry_suffixes:
        candidates.append((TWSE_OPENAPI, f"/opendata/t187ap06_L_{suffix}", "TWSE/MOPS Income Statement", "上市", "detail"))
        # X_* is the public-company/foreign-issuer feed and covers cases that do not appear in L_* as expected.
        candidates.append((TWSE_OPENAPI, f"/opendata/t187ap06_X_{suffix}", "TWSE/MOPS Income Statement (X/foreign)", "公發/外國", "detail"))
        candidates.append((TPEX_OPENAPI, f"/mopsfin_t187ap06_O_{suffix}", "TPEx/MOPS Income Statement", "上櫃", "detail"))
    errors=[]; found=[]

    # Layer 0: company-specific MOPS IFRS report for the expected quarter.
    # Aggregate OpenAPI feeds can be incomplete/staggered; direct company report is authoritative.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        found.extend(await fetch_mops_company_ifrs(ticker,ey,eq))
    except Exception as e:
        errors.append(f"mops_company:{type(e).__name__}")

    # TSMC publishes quarterly results earlier than some MOPS aggregate refreshes.
    if ticker=="2330":
        try:
            ey,eq,_=expected_latest_financial_period(date.today())
            ir=await fetch_tsmc_quarterly_release(ey,eq)
            if ir: found.append(ir)
        except Exception as e:
            errors.append(f"tsmc_ir:{type(e).__name__}")

    def company_code(row: dict[str, Any]) -> str:
        direct=row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("公司代碼") or row.get("代號")
        if direct is not None: return re.sub(r"\D", "", str(direct)) or str(direct).strip()
        v=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
        return str(v or "").strip()

    async def probe(base: str, path: str, source: str, market: str, kind: str):
        try:
            rows=await openapi_json(base,path)
            row=next((x for x in rows if company_code(x)==ticker),None)
            if not row: return None
            eps=parse_num_text(_row_value(row,["基本每股盈餘"]) or _row_value(row,["每股盈餘"]))
            revenue=parse_num_text(_row_value(row,["營業收入"],["百分比"]))
            gross=parse_num_text(_row_value(row,["營業毛利"],["百分比"]))
            op=parse_num_text(_row_value(row,["營業利益"],["百分比"]))
            net=parse_num_text(_row_value(row,["本期淨利"],["百分比"]) or _row_value(row,["本期稅後淨利"]))
            year=_row_value(row,["年度"]) or _row_value(row,["年"])
            quarter=_row_value(row,["季別"]) or _row_value(row,["季"])
            out_date=_row_value(row,["出表日期"]) or _row_value(row,["資料日期"])
            try:
                y=int(str(year).strip()); y=y+1911 if y<1911 else y
            except Exception: y=None
            q=None; qm=re.search(r"([1-4])",str(quarter or "")); q=int(qm.group(1)) if qm else None
            period=f"{y} Q{q}" if y and q else (roc_date_to_iso(out_date) or "latest")
            return {"source":source,"market":market,"endpoint":path,"official":True,"period":period,
                    "fiscal_year":y,"fiscal_quarter":q,"statement_date":roc_date_to_iso(out_date),
                    "ytd_eps":eps,"revenue_ytd":revenue,"gross_profit_ytd":gross,
                    "operating_income_ytd":op,"net_income_ytd":net,"raw_keys":list(row.keys())[:20],
                    "feed_kind":kind,
                    "completeness":sum(v is not None for v in (eps,revenue,gross,op,net))}
        except Exception as e:
            errors.append(f"{path}:{type(e).__name__}")
            return None

    results=await asyncio.gather(*(probe(*c) for c in candidates))
    found.extend(x for x in results if x)
    # Layer 2: direct official MOPS CSV. This bypasses JSON endpoint/schema lag and is critical for KY/foreign issuers.
    try:
        found.extend(await fetch_mops_csv_official(ticker))
    except Exception as e:
        errors.append(f"mops_csv:{type(e).__name__}")
    # Layer 3: official board-approved financial-report disclosure. This is especially important for
    # KY/foreign issuers whose structured XBRL/EPS feed can lag even after the board has approved Q2/Q3.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        found.extend(await fetch_mops_material_financial(ticker,ey,eq))
    except Exception as e:
        errors.append(f"mops_material:{type(e).__name__}")

    # Layer 4: company IR reviewed/audited PDF for mapped issuers. Used only when the PDF explicitly states the expected period.
    try:
        ey,eq,_=expected_latest_financial_period(date.today())
        ir=await fetch_company_ir_financial(ticker,ey,eq)
        if ir: found.append(ir)
    except Exception as e:
        errors.append(f"company_ir:{type(e).__name__}")
    if found:
        # Latest fiscal period always wins. Within the same period prefer the detailed row with more parsed fields.
        found.sort(key=lambda x: (x.get("fiscal_year") or 0, x.get("fiscal_quarter") or 0,
                                  x.get("completeness") or 0, 1 if x.get("feed_kind")=="detail" else 0,
                                  x.get("statement_date") or ""), reverse=True)
        best=found[0]
        latest_key=(best.get("fiscal_year"),best.get("fiscal_quarter"))
        same=[x for x in found if (x.get("fiscal_year"),x.get("fiscal_quarter"))==latest_key]
        # Merge fields across official feeds for the same period; e.g. daily EPS summary can anchor Q2 while
        # a detailed feed supplies gross profit/margins. Never merge across different fiscal periods.
        merged=dict(best)
        for row in same:
            for k in ("ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","statement_date",
                      "quarter_eps_direct","gross_margin_direct","operating_margin_direct"):
                if merged.get(k) is None and row.get(k) is not None:
                    merged[k]=row[k]
        merged["source_candidates"]=[{"source":x.get("source"),"endpoint":x.get("endpoint"),"kind":x.get("feed_kind"),"completeness":x.get("completeness")} for x in same]
        merged["errors"]=errors; merged["candidate_hits"]=len(found)
        return merged
    return {"official":False,"errors":errors,"candidate_hits":0}



async def diagnose_official_financial_sources(ticker: str) -> dict[str, Any]:
    """Live diagnostic of every official-financial source used by V5.2.4.

    This endpoint intentionally exposes metadata/status only (no secrets). It is designed to answer:
    * did the upstream endpoint respond?
    * did it contain the requested ticker?
    * what fiscal period did it report?
    * for company IR, did the page advertise the expected quarter and did it expose a PDF link?
    * if a PDF was found, did its text explicitly validate the expected period and parse the core fields?
    """
    now=datetime.now().astimezone()
    ey,eq,expected=expected_latest_financial_period(now.date())
    ua={"User-Agent":"Mozilla/5.0 AI-Stock-Research/5.2.7 diagnostics"}
    out={
        "ticker":ticker, "generated_at":now.isoformat(timespec="seconds"),
        "expected_period":expected, "expected_year":ey, "expected_quarter":eq,
        "company_mops":[], "openapi":[], "mops_csv":[], "mops_material":[], "company_ir":None, "selection":None,
        "diagnostic_version":"5.2.5",
    }

    def company_code(row: dict[str, Any]) -> str:
        direct=row.get("公司代號") or row.get("SecuritiesCompanyCode") or row.get("公司代碼") or row.get("代號")
        if direct is not None:
            return re.sub(r"\D","",str(direct)) or str(direct).strip()
        v=_row_value(row,["公司","代號"]) or _row_value(row,["代號"])
        return str(v or "").strip()

    industry_suffixes=["ci","mim","basi","bd","fh","ins"]
    candidates=[
        (TWSE_OPENAPI, "/opendata/t187ap14_L", "TWSE/MOPS EPS Daily Summary", "summary"),
        (TPEX_OPENAPI, "/mopsfin_t187ap14_O", "TPEx/MOPS EPS Daily Summary", "summary"),
    ]
    for suffix in industry_suffixes:
        candidates += [
            (TWSE_OPENAPI, f"/opendata/t187ap06_L_{suffix}", "TWSE/MOPS Income Statement", "detail"),
            (TWSE_OPENAPI, f"/opendata/t187ap06_X_{suffix}", "TWSE/MOPS X/Foreign Income Statement", "detail"),
            (TPEX_OPENAPI, f"/mopsfin_t187ap06_O_{suffix}", "TPEx/MOPS Income Statement", "detail"),
        ]

    # Company-specific MOPS IFRS diagnostics (the primary V5.2.5 source).
    try:
        direct=await fetch_mops_company_ifrs(ticker,ey,eq)
        out["company_mops"]=[{k:x.get(k) for k in ("source","endpoint","period","fiscal_year","fiscal_quarter","report_id","selected_column","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness","table_count")} for x in direct]
    except Exception as e:
        out["company_mops_error"]=f"{type(e).__name__}: {str(e)[:240]}"

    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=ua) as client:
        for base,path,source,kind in candidates:
            item={"source":source,"url":base+path,"kind":kind}
            try:
                r=await client.get(base+path)
                item.update({"http_status":r.status_code,"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                data=r.json()
                rows=data if isinstance(data,list) else []
                item["rows_count"]=len(rows)
                row=next((x for x in rows if company_code(x)==ticker),None)
                item["matched"]=bool(row)
                if row:
                    snap=_official_row_to_snapshot(row,source,path,"official",kind)
                    item["snapshot"]={k:snap.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness")} if snap else None
                    item["raw_keys"]=list(row.keys())[:40]
            except Exception as e:
                item["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["openapi"].append(item)

        csv_files=[
            ("t187ap14_L.csv","TWSE/MOPS EPS CSV","summary"),
            ("t187ap06_L_ci.csv","TWSE/MOPS Income CSV","detail"),
            ("t187ap06_L_mim.csv","TWSE/MOPS Income CSV","detail"),
            ("t187ap06_X_ci.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
            ("t187ap06_X_mim.csv","TWSE/MOPS Public/Foreign Income CSV","detail"),
        ]
        for filename,source,kind in csv_files:
            url=f"{MOPS_CSV_BASE}/{filename}"
            item={"source":source,"url":url,"kind":kind}
            try:
                r=await client.get(url)
                item.update({"http_status":r.status_code,"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                text=None; encoding=None
                for enc in ("utf-8-sig","utf-8","cp950","big5"):
                    try: text=r.content.decode(enc); encoding=enc; break
                    except Exception: pass
                if text is None: raise ValueError("CSV decode failed")
                rows=[dict(x) for x in csv.DictReader(io.StringIO(text))]
                item.update({"encoding":encoding,"rows_count":len(rows),"header":list(rows[0].keys())[:40] if rows else []})
                matched=None; snap=None
                for row in rows:
                    candidate=_official_row_to_snapshot(row,source,filename,"official",kind)
                    if candidate and candidate.get("company_code")==ticker:
                        matched=row; snap=candidate; break
                item["matched"]=bool(matched)
                if snap:
                    item["snapshot"]={k:snap.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness")}
            except Exception as e:
                item["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["mops_csv"].append(item)

        # Official material-information fallback diagnostics.
        try:
            mats=await fetch_mops_material_financial(ticker,ey,eq)
            out["mops_material"]=[{k:x.get(k) for k in ("period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","completeness","subject","source")} for x in mats]
        except Exception as e:
            out["mops_material_error"]=f"{type(e).__name__}: {str(e)[:240]}"

        page=IR_FINANCIAL_PAGES.get(ticker)
        if page:
            ir={"page_url":page,"expected_period":expected,"pdf_candidates":[]}
            try:
                r=await client.get(page)
                html_text=r.text
                ir.update({"http_status":r.status_code,"final_url":str(r.url),"content_type":r.headers.get("content-type"),"bytes":len(r.content)})
                r.raise_for_status()
                compact_html=re.sub(r"\s+"," ",html_text)
                q_tokens={1:["Q1","First Quarter","第一季度","第一季"],2:["Q2","Second Quarter","第二季度","第二季"],3:["Q3","Third Quarter","第三季度","第三季"],4:["Q4","Fourth Quarter","第四季度","第四季"]}[eq]
                ir["expected_year_visible"]=str(ey) in html_text
                ir["expected_quarter_label_visible"]=any(t.lower() in html_text.lower() for t in q_tokens)
                year_pos=compact_html.find(str(ey))
                ir["html_context"]=compact_html[max(0,year_pos-160):year_pos+700] if year_pos>=0 else compact_html[:700]
                hrefs=re.findall(r'href=["\']([^"\']+)["\']',html_text,re.I)
                embedded=re.findall(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',html_text,re.I)
                pdfs=[]
                for href in list(dict.fromkeys(hrefs+embedded)):
                    if ".pdf" not in href.lower(): continue
                    url=urljoin(str(r.url),href); lo=url.lower(); score=0
                    if str(ey) in lo: score+=4
                    if f"q{eq}" in lo or f"_{eq}_" in lo: score+=5
                    pdfs.append((score,url))
                pdfs=sorted(set(pdfs),reverse=True)[:16]
                ir["pdf_link_count"]=len(pdfs)
                ir["expected_q_pdf_link_detected"]=any(score>=5 for score,_ in pdfs)
                for score,url in pdfs[:8]:
                    pi={"url":url,"score":score}
                    try:
                        pr=await client.get(url)
                        pi.update({"http_status":pr.status_code,"content_type":pr.headers.get("content-type"),"bytes":len(pr.content)})
                        pr.raise_for_status()
                        reader=PdfReader(io.BytesIO(pr.content))
                        text="\n".join((pg.extract_text() or "") for pg in reader.pages[:12])
                        low=text.lower()
                        if eq==2: period_ok=(str(ey) in text and ("six months ended june 30" in low or "六月三十日" in text or "6月30日" in text))
                        elif eq==1: period_ok=(str(ey) in text and ("three months ended march 31" in low or "三月三十一日" in text or "3月31日" in text))
                        elif eq==3: period_ok=(str(ey) in text and ("nine months ended september 30" in low or "九月三十日" in text or "9月30日" in text))
                        else: period_ok=(str(ey) in text and ("year ended december 31" in low or "十二月三十一日" in text or "12月31日" in text))
                        pi.update({"pages":len(reader.pages),"text_chars_first12":len(text),"period_ok":period_ok})
                        if period_ok: pi["parsed"]=_extract_pdf_financials(text,ey,eq)
                    except Exception as e:
                        pi["error"]=f"{type(e).__name__}: {str(e)[:240]}"
                    ir["pdf_candidates"].append(pi)
            except Exception as e:
                ir["error"]=f"{type(e).__name__}: {str(e)[:240]}"
            out["company_ir"]=ir

    try:
        selected=await fetch_official_income_statement(ticker)
        out["selection"]={k:selected.get(k) for k in ("official","source","endpoint","period","fiscal_year","fiscal_quarter","statement_date","ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd","candidate_hits","errors")}
    except Exception as e:
        out["selection"]={"error":f"{type(e).__name__}: {str(e)[:240]}"}

    # concise machine-readable conclusion for screenshots/support
    matched_open=[x for x in out["openapi"] if x.get("matched")]
    matched_csv=[x for x in out["mops_csv"] if x.get("matched")]
    ir=out.get("company_ir") or {}
    valid_pdf=[x for x in ir.get("pdf_candidates",[]) if x.get("period_ok")]
    out["summary"]={
        "company_mops_hits":len(out.get("company_mops") or []), "openapi_hits":len(matched_open), "csv_hits":len(matched_csv), "material_financial_hits":len(out.get("mops_material") or []), "valid_ir_pdfs":len(valid_pdf),
        "ir_expected_label_visible":ir.get("expected_quarter_label_visible"),
        "ir_expected_pdf_link_detected":ir.get("expected_q_pdf_link_detected"),
        "selected_period":(out.get("selection") or {}).get("period"),
        "selected_source":(out.get("selection") or {}).get("source"),
    }
    return out


async def reconcile_official_financial_snapshot(ticker: str, selected: dict[str, Any]) -> dict[str, Any]:
    """Final safety reconciliation before data reaches /api/stock.

    Diagnostics proved that official MOPS CSV can already contain a newer quarter even when
    another upstream path or stale structured source wins earlier in the request. This helper
    independently re-checks the official CSV and forces the latest official fiscal period into
    the main stock payload. Fields are merged only within the same fiscal period.
    """
    candidates: list[dict[str, Any]] = []
    if selected and selected.get("official"):
        candidates.append(dict(selected))
    try:
        candidates.extend(await fetch_mops_csv_official(ticker))
    except Exception:
        pass
    if not candidates:
        return selected or {"official": False, "candidate_hits": 0, "errors": ["reconcile:no_official_candidate"]}

    def key(x: dict[str, Any]):
        return (int(x.get("fiscal_year") or 0), int(x.get("fiscal_quarter") or 0))
    latest_key=max(key(x) for x in candidates)
    latest=[x for x in candidates if key(x)==latest_key]
    latest.sort(key=lambda x:(x.get("completeness") or 0, 1 if x.get("feed_kind")=="detail" else 0, x.get("statement_date") or ""), reverse=True)
    merged=dict(latest[0])
    for row in latest[1:]:
        for field in ("ytd_eps","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd",
                      "quarter_eps_direct","gross_margin_direct","operating_margin_direct","statement_date"):
            if merged.get(field) is None and row.get(field) is not None:
                merged[field]=row[field]
    merged["official"]=True
    merged["period"]=f"{latest_key[0]} Q{latest_key[1]}" if all(latest_key) else merged.get("period")
    merged["mapping_reconciled"]=True
    merged["mapping_sources"]=[{"source":x.get("source"),"endpoint":x.get("endpoint"),"period":x.get("period"),"completeness":x.get("completeness")} for x in latest]
    return merged

def expected_latest_financial_period(as_of: date | None=None) -> tuple[int,int,str]:
    """Conservative Taiwan quarterly filing calendar used only as a freshness gate.

    It does not invent financial values. It decides whether the latest fetched period is old enough
    that the UI/valuation must warn or block stale accounting data.
    """
    d=as_of or date.today(); y=d.year
    md=(d.month,d.day)
    if md >= (11,14): return y,3,f"{y} Q3"
    if md >= (8,14): return y,2,f"{y} Q2"
    if md >= (5,15): return y,1,f"{y} Q1"
    if md >= (3,31): return y-1,4,f"{y-1} Q4"
    return y-1,3,f"{y-1} Q3"


def assess_financial_integrity(official: dict[str, Any], eps_stack: dict[str, Any], as_of: date | None=None) -> dict[str, Any]:
    ey,eq,expected=expected_latest_financial_period(as_of)
    expected_key=(ey,eq)
    oy,oq=official.get("fiscal_year"),official.get("fiscal_quarter")
    official_key=(oy,oq) if oy and oq else None
    api_period=eps_stack.get("structured_api_period")
    am=re.match(r"(\d{4}) Q([1-4])",str(api_period or ""))
    api_key=(int(am.group(1)),int(am.group(2))) if am else None
    verified=bool(official.get("official") and official_key and official_key>=expected_key)
    official_stale=bool(official.get("official") and official_key and official_key<expected_key)
    api_stale=bool(api_key and api_key<expected_key)
    eps_ready=verified and (eps_stack.get("ytd_eps") is not None or eps_stack.get("quarter_eps") is not None)
    if verified and eps_ready:
        status="verified"; severity="ok"; message=f"官方最新財報期已驗證：{official.get('period')}"
    elif verified:
        status="verified_period_missing_eps"; severity="warning"; message=f"官方期間已達 {official.get('period')}，但 EPS 欄位尚未可靠解析；核心財報估值暫停。"
    elif official_stale:
        status="official_stale"; severity="stale"; message=f"官方介面目前僅取得 {official.get('period')}，低於應有 {expected}；不得標示為最新。"
    else:
        status="unverified"; severity="stale"; message=f"尚未從官方介面驗證 {expected}；結構化 API 財報不得視為最新。"
    return {"status":status,"severity":severity,"expected_period":expected,
            "official_period":official.get("period"),"structured_api_period":api_period,
            "official_verified":verified,"official_stale":official_stale,"structured_api_stale":api_stale,
            "core_financials_allowed":eps_ready,"market_per_is_independent":True,"message":message,
            "rule":"只有官方期間達到應有季度且至少能解析 YTD EPS 或官方單季 EPS，財報 EPS 才能進核心估值；法人明確年度 Forward EPS 可獨立使用。"}

def _quarter_from_date(v: Any) -> tuple[int|None,int|None]:
    try:
        d=pd.to_datetime(v); return int(d.year), int((d.month-1)//3+1)
    except Exception: return None,None

def finmind_eps_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    df=pd.DataFrame(rows)
    if df.empty or not {"date","type","value"}.issubset(df.columns): return []
    df["value"]=pd.to_numeric(df["value"],errors="coerce"); df=df.dropna(subset=["value"])
    mask=df["type"].astype(str).str.contains("BasicEarningsPerShare|EarningsPerShare|基本每股盈餘",case=False,regex=True)
    e=df[mask].copy()
    out=[]
    for dt,g in e.groupby("date"):
        y,q=_quarter_from_date(dt); val=safe_num(g.iloc[0]["value"]);
        if y and q and val is not None: out.append({"date":str(dt),"year":y,"quarter":q,"ytd_eps":val,"source":"FinMind"})
    return sorted(out,key=lambda x:x["date"])

def _best_period_snapshot(rows: list[dict[str, Any]], year: int, quarter: int) -> dict[str, Any] | None:
    candidates=[r for r in rows if r.get("official") and r.get("fiscal_year")==year and r.get("fiscal_quarter")==quarter]
    if not candidates: return None
    candidates.sort(key=lambda r:(r.get("completeness") or 0, r.get("statement_date") or ""), reverse=True)
    best=dict(candidates[0])
    for r in candidates[1:]:
        for k in ("ytd_eps","quarter_eps_direct","revenue_ytd","gross_profit_ytd","operating_income_ytd","net_income_ytd"):
            if best.get(k) is None and r.get(k) is not None: best[k]=r[k]
    return best

async def fetch_official_eps_ytd_for_period(ticker: str, year: int, quarter: int) -> dict[str, Any] | None:
    """Fetch official cumulative EPS for one exact fiscal period.

    Company-specific MOPS IFRS is primary because aggregate feeds only expose the latest batch.
    For the current period, the already-selected official snapshot is used by the caller. This
    helper is for historical predecessor quarters needed to derive true single-quarter EPS/TTM.
    """
    rows=[]
    try:
        rows.extend(await fetch_mops_company_ifrs(ticker, year, quarter))
    except Exception:
        pass
    best=_best_period_snapshot(rows,year,quarter)
    if best and best.get("ytd_eps") is not None: return best
    # TSMC company IR may expose direct quarter EPS. Keep it as a direct-quarter fallback,
    # but never convert it to cumulative YTD here.
    if ticker=="2330":
        try:
            ir=await fetch_tsmc_quarterly_release(year,quarter)
            if ir and ir.get("quarter_eps_direct") is not None: return ir
        except Exception:
            pass
    return best

async def build_eps_stack(ticker: str, fin_rows: list[dict[str, Any]], official: dict[str, Any], fallback_financial: dict[str, Any]) -> dict[str, Any]:
    """Official-first EPS engine.

    * Q1 single-quarter EPS = Q1 YTD EPS.
    * Q2/Q3 = current YTD - prior-quarter YTD from official MOPS.
    * Q4 = full-year YTD - Q3 YTD.
    * TTM = sum of four actual single-quarter EPS values only.
    FinMind is retained only as a labeled fallback when official history cannot be obtained.
    """
    fin_hist=finmind_eps_history(fin_rows)
    fy=official.get("fiscal_year") if official.get("official") else None
    fq=official.get("fiscal_quarter") if official.get("official") else None
    ytd=official.get("ytd_eps") if official.get("official") else None
    source="TWSE/MOPS official" if official.get("official") else "FinMind"
    if ytd is None and fin_hist:
        last=fin_hist[-1]; fy,fq,ytd=last["year"],last["quarter"],last["ytd_eps"]; source=last["source"]

    # Build an official cumulative-EPS map for enough periods to derive the latest quarter and TTM.
    official_ytd={}
    direct_quarter={}
    if fy and fq and official.get("official"):
        if ytd is not None: official_ytd[(fy,fq)]=float(ytd)
        if official.get("quarter_eps_direct") is not None: direct_quarter[(fy,fq)]=float(official.get("quarter_eps_direct"))
        periods=[]
        y,q=fy,fq
        # Need latest 5 cumulative points to derive the last four standalone quarters.
        for _ in range(5):
            periods.append((y,q))
            q-=1
            if q==0: y-=1; q=4
        # Fetch exact historical MOPS quarters in parallel, excluding current which is already selected.
        tasks=[fetch_official_eps_ytd_for_period(ticker,y,q) for (y,q) in periods[1:]]
        rows=await asyncio.gather(*tasks, return_exceptions=True)
        for (yq,row) in zip(periods[1:],rows):
            if isinstance(row,Exception) or not row: continue
            if row.get("ytd_eps") is not None: official_ytd[yq]=float(row.get("ytd_eps"))
            if row.get("quarter_eps_direct") is not None: direct_quarter[yq]=float(row.get("quarter_eps_direct"))

    # Structured API history is a fallback only; do not overwrite an official value.
    ytd_map=dict(official_ytd)
    ytd_source={k:"official" for k in official_ytd}
    for x in fin_hist:
        key=(x["year"],x["quarter"])
        if key not in ytd_map:
            ytd_map[key]=float(x["ytd_eps"]); ytd_source[key]="FinMind"

    def standalone(year:int, quarter:int):
        key=(year,quarter)
        if key in direct_quarter: return direct_quarter[key], "official_direct"
        cur=ytd_map.get(key)
        if cur is None: return None,None
        if quarter==1: return cur,ytd_source.get(key)
        prev=ytd_map.get((year,quarter-1))
        if prev is None: return None,None
        # Require official predecessor when current is official; otherwise we would mix stale API into an official quarter.
        if ytd_source.get(key)=="official" and ytd_source.get((year,quarter-1))!="official": return None,None
        return cur-prev, "official_derived" if ytd_source.get(key)=="official" else "structured_api_derived"

    quarter_eps=None; quarter_method=None
    if fy and fq:
        quarter_eps,quarter_method=standalone(fy,fq)

    quarters=[]
    if fy and fq:
        y,q=fy,fq
        last4=[]
        for _ in range(4):
            last4.append((y,q)); q-=1
            if q==0: y-=1; q=4
        for y,q in reversed(last4):
            val,method=standalone(y,q)
            if val is not None:
                quarters.append({"year":y,"quarter":q,"eps":val,"period":f"{y} Q{q}","method":method})
    ttm=sum(x["eps"] for x in quarters) if len(quarters)==4 else None
    latest_period=f"{fy} Q{fq}" if fy and fq else fallback_financial.get("statement_date")
    api_latest=(fin_hist[-1]["year"],fin_hist[-1]["quarter"]) if fin_hist else (None,None)
    official_key=(fy,fq) if fy and fq and official.get("official") else (None,None)
    stale_api=bool(official_key[0] and api_latest[0] and official_key>api_latest)
    prev_key=(fy,fq-1) if fy and fq and fq>1 else ((fy-1,4) if fy and fq else None)
    return {
        "quarter_eps":quarter_eps,"quarter_period":latest_period,"quarter_method":quarter_method,
        "ytd_eps":ytd,"ytd_period":f"{fy} Q{fq} YTD" if fy and fq else latest_period,
        "ttm_eps":ttm,"ttm_period":latest_period,"source":source,"official_period":official.get("period"),
        "history":quarters,"quality":"official_eps_engine" if official.get("official") else "structured_api",
        "structured_api_stale":stale_api,"structured_api_period":f"{api_latest[0]} Q{api_latest[1]}" if api_latest[0] else None,
        "prior_ytd_period":f"{prev_key[0]} Q{prev_key[1]} YTD" if prev_key else None,
        "prior_ytd_eps":ytd_map.get(prev_key) if prev_key else None,
        "note":"V5.2.7 EPS Engine：Q1=累計；Q2/Q3=本期累計－前季累計；Q4=全年累計－Q3累計。TTM 只加總四個實際單季 EPS；官方當期若缺官方前季，不混用舊 API 推算。"
    }


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

def _extract_eps_year(text: str) -> int | None:
    # Only annual forward EPS with an explicit forecast year is eligible for valuation consensus.
    pats=[r"(20\d{2})(?:E|e|年|年度)?[^。；\n]{0,24}(?:EPS|每股盈餘)", r"(?:EPS|每股盈餘)[^。；\n]{0,24}(20\d{2})(?:E|e|年|年度)?"]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            y=int(m.group(1))
            if date.today().year-1 <= y <= date.today().year+4: return y
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
            inst=_extract_institution(text); target=_extract_target(text); rating=_extract_rating(text); eps=_extract_eps(text); eps_year=_extract_eps_year(text)
            if not any([inst,target,rating,eps]): continue
            score=35 + (25 if inst else 0) + (25 if target else 0) + (10 if rating else 0) + (5 if eps else 0)
            rows.append({
                "institution":inst or "未辨識機構", "report_date":x['published_date'], "rating":rating,
                "target_price":target, "forward_eps":eps, "eps_year":eps_year, "title":x['title'], "summary":x['snippet'][:220],
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
    # Annual EPS consensus must have an explicit forecast year. Mixing quarterly/TTM/annual EPS is prohibited.
    eps_by_year: dict[int,list[float]]={}
    for x in rows:
        ep=safe_num(x.get('forward_eps')); ey=x.get('eps_year')
        if ep is not None and isinstance(ey,int) and x.get('confidence',0)>=70:
            eps_by_year.setdefault(ey,[]).append(ep)
    eps_consensus={str(y):median(vals) for y,vals in sorted(eps_by_year.items()) if vals}
    current_year=date.today().year
    chosen_year=next((y for y in sorted(eps_by_year) if y>=current_year),None)
    epss=eps_by_year.get(chosen_year,[]) if chosen_year else []
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
        "median_forward_eps":median(epss) if epss else None, "forward_eps_year":chosen_year, "forward_eps_by_year":eps_consensus, "eps_coverage":len(epss), "target_revision_pct":median(revisions) if revisions else None, "eps_revision_pct":median(eps_revisions) if eps_revisions else None,
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


def model_valuation(price: float | None, perdata: dict[str, Any], eps_stack: dict[str, Any], research: dict[str, Any], integrity: dict[str, Any] | None=None) -> dict[str, Any]:
    if not price: return {"scenarios": [], "confidence": 0}
    consensus_eps=safe_num(research.get("median_forward_eps")); coverage=int(research.get("eps_coverage") or 0)
    integrity=integrity or {}; allow_financial=bool(integrity.get("core_financials_allowed", True))
    ttm=safe_num(eps_stack.get("ttm_eps")) if allow_financial else None
    ytd=safe_num(eps_stack.get("ytd_eps")) if allow_financial else None
    q=eps_stack.get("quarter_period")
    if consensus_eps and coverage >= 2:
        anchor_eps=consensus_eps; eps_basis=f"{research.get('forward_eps_year') or 'Forward'}E EPS 中位數（{coverage} 筆明確年度可比預估）"; eps_conf=82
    elif ttm and ttm>0:
        anchor_eps=ttm; eps_basis=f"TTM EPS {ttm:.2f}（截至 {q or 'latest'}；不使用單季×4）"; eps_conf=78
    elif ytd and ytd>0:
        anchor_eps=ytd; eps_basis=f"YTD EPS {ytd:.2f}（資料不足以形成 TTM，暫不年化單季）"; eps_conf=48
    elif perdata.get("per") and perdata["per"]>0 and allow_financial:
        anchor_eps=price/perdata["per"]; eps_basis="由現價 / 市場 PER 反推 TTM EPS（降級模型）"; eps_conf=32
    else: return {"scenarios": [], "eps_basis":"資料不足", "confidence":0}
    if perdata.get("pe_median"):
        bear_pe=max(5.0,perdata["pe_p25"]); base_pe=perdata["pe_median"]; bull_pe=min(150.0,perdata["pe_p75"]); pe_basis=f"近年歷史 PER 分位數（樣本 {perdata.get('sample_count',0)} 日）"; pe_conf=85
    else:
        center=perdata.get("per") if perdata.get("per") and 5<=perdata["per"]<=120 else 20.0; bear_pe,base_pe,bull_pe=max(8.0,center*.8),center,min(150.0,center*1.2); pe_basis="目前 PER ±20%（歷史樣本不足降級模型）"; pe_conf=50
    scenarios=[{"name":"悲觀","eps":anchor_eps*.90,"pe":bear_pe},{"name":"基準","eps":anchor_eps,"pe":base_pe},{"name":"樂觀","eps":anchor_eps*1.10,"pe":bull_pe}]
    for x in scenarios: x["target"]=x["eps"]*x["pe"]; x["upside_pct"]=(x["target"]/price-1)*100
    return {"eps_basis":eps_basis,"pe_basis":pe_basis,"confidence":round((eps_conf+pe_conf)/2),"anchor_eps":anchor_eps,"scenarios":scenarios}


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
    try:
        official_financial=await fetch_official_income_statement(ticker)
    except Exception as e:
        errors.append(f"OfficialFinancial: {type(e).__name__}"); official_financial={"official":False,"errors":[type(e).__name__]}
    # V5.2.7 official mapping + EPS engine guard: force the newest official MOPS quarter into the main payload.
    try:
        official_financial=await reconcile_official_financial_snapshot(ticker, official_financial)
    except Exception as e:
        errors.append(f"OfficialReconcile: {type(e).__name__}")
    eps_stack=await build_eps_stack(ticker, fin, official_financial, financial)
    financial_integrity=assess_financial_integrity(official_financial, eps_stack, today)
    # Official snapshot has highest priority for latest period margins/amounts.
    if official_financial.get("official"):
        financial.update({
            "statement_date":official_financial.get("statement_date") or official_financial.get("period"),
            "period":official_financial.get("period"), "source":official_financial.get("source"), "official":True,
            "ytd_eps":official_financial.get("ytd_eps"), "quarter_eps":eps_stack.get("quarter_eps"), "ttm_eps":eps_stack.get("ttm_eps"),
            "revenue":official_financial.get("revenue_ytd") if official_financial.get("revenue_ytd") is not None else financial.get("revenue"),
            "gross_profit":official_financial.get("gross_profit_ytd") if official_financial.get("gross_profit_ytd") is not None else financial.get("gross_profit"),
            "operating_income":official_financial.get("operating_income_ytd") if official_financial.get("operating_income_ytd") is not None else financial.get("operating_income"),
            "net_income":official_financial.get("net_income_ytd") if official_financial.get("net_income_ytd") is not None else financial.get("net_income"),
        })
        if financial.get("revenue"):
            financial["gross_margin"]=(financial.get("gross_profit")/financial["revenue"]*100) if financial.get("gross_profit") is not None else financial.get("gross_margin")
            financial["operating_margin"]=(financial.get("operating_income")/financial["revenue"]*100) if financial.get("operating_income") is not None else financial.get("operating_margin")
            financial["net_margin"]=(financial.get("net_income")/financial["revenue"]*100) if financial.get("net_income") is not None else financial.get("net_margin")
        if official_financial.get("gross_margin_direct") is not None:
            financial["gross_margin"]=official_financial.get("gross_margin_direct")
        if official_financial.get("operating_margin_direct") is not None:
            financial["operating_margin"]=official_financial.get("operating_margin_direct")
    if not financial_integrity.get("official_verified"):
        financial["official"]=False
        financial["source"]=(financial.get("source") or "FinMind") + "（未通過官方最新季度驗證）"
        financial["integrity_warning"]=financial_integrity.get("message")
    company_name=info.get("stock_name") or ticker
    try:
        web_research, company_events = await asyncio.gather(fetch_public_research(ticker, company_name), fetch_company_events(ticker, company_name))
    except Exception as e:
        errors.append(f"PublicWebResearch: {type(e).__name__}"); web_research={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}; company_events={"rows":[],"errors":[type(e).__name__],"fetched_at":datetime.now().astimezone().isoformat(timespec="seconds")}
    research=merge_research(load_research(ticker), web_research.get("rows", []))
    lp=tech.get("last")
    valuation=model_valuation(lp,perdata,eps_stack,research,financial_integrity)
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
        {"name":"財務報表","dataset":financial.get("source") or "FinMind TaiwanStockFinancialStatements","as_of":financial.get("period") or financial.get("statement_date"),"status":"ok" if financial_integrity.get("official_verified") else "stale","scheduled_update":"僅官方最新季度驗證通過才標示 OK"},
        {"name":"財報完整性閘門","dataset":"Official period gate","as_of":financial_integrity.get("official_period") or eps_stack.get("structured_api_period"),"status":"ok" if financial_integrity.get("core_financials_allowed") else "stale","scheduled_update":financial_integrity.get("message")},
        {"name":"公開法人研究","dataset":"Google News RSS + 公開網路引用","as_of":web_research.get("fetched_at"),"status":"ok" if web_research.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
        {"name":"公司事件雷達","dataset":"公開新聞/法說/重大訊息引用","as_of":company_events.get("fetched_at"),"status":"ok" if company_events.get("rows") else "missing","scheduled_update":"每次強制刷新重新搜尋"},
    ]
    conf=calc_confidence(source_status,valuation,research)
    data={"ticker":ticker,"name":info.get("stock_name") or ticker,"industry":info.get("industry_category") or "—","market_type":info.get("type") or "—",
          "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),"price":lp,"change_pct":change,"technical":tech,"revenue":revenue,"flow":flow,"per":perdata,"financial":financial,
          "research":research,"expectation_gap":expectation,"valuation":valuation,"eps_stack":eps_stack,"official_financial":official_financial,"financial_integrity":financial_integrity,"scores":sc,"stance":nar["stance"],"thesis":nar["thesis"],"catalysts":nar["catalysts"],"risks":nar["risks"],"confidence":conf,
          "source_status":source_status,"errors":errors,"cache":{"hit":False,"ttl_seconds":CACHE_TTL},"web_research_meta":web_research,"company_events":company_events,
          "data_policy":"V5.2.7 EPS Engine：官方財報先映射最新季度，再回查前一季官方累計 EPS 計算真實單季 EPS；TTM 僅由四個實際單季 EPS 加總，不做年化猜值。"}
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
    <div class='head'><div><div class='muted'>AI STOCK RESEARCH TERMINAL V5.2.5 • TAIWAN EQUITY RESEARCH</div><h1>{esc(d['name'])} <span class='muted'>{esc(d['ticker'])}</span></h1><div><span class='pill'>{esc(d['industry'])}</span><span class='pill'>{esc(d['stance'])}</span><span class='pill'>可信度 {conf['overall']}/100</span></div></div><div><div class='muted'>最新收盤</div><div class='price'>{nfmt(d['price'],1)}</div><div>{pct(d['change_pct'])}</div></div></div>
    <div class='small muted'>報告產生：{esc(d['generated_at'])} ｜ 資料完整度：{conf['data_completeness']}% ｜ 估值信心：{conf['valuation_confidence']}%</div>
    <h2>1. Executive Summary</h2><div class='call'>{esc(d['thesis'])}</div>
    <div class='grid'><div class='card'>綜合評分<b>{sc['綜合']}/100</b></div><div class='card'>基本面<b>{sc['基本面']}</b></div><div class='card'>籌碼面<b>{sc['籌碼面']}</b></div><div class='card'>技術面<b>{sc['技術面']}</b></div></div>
    <div class='cols'><div><b>主要催化劑</b><ul>{cats}</ul></div><div><b>主要風險</b><ul>{risks}</ul></div></div>
    <h2>2. Expectation Gap & Revision Radar</h2><div class='call'><b>{esc(exp.get('regime'))}</b><br>{esc(exp.get('summary'))}</div><div class='grid'><div class='card'>預期修正分數<b>{exp.get('revision_score','—')}/100</b></div><div class='card'>EPS 修正<b>{pct(research.get('eps_revision_pct'))}</b></div><div class='card'>目標價修正<b>{pct(research.get('target_revision_pct'))}</b></div><div class='card'>估值區域<b style='font-size:10pt'>{esc(exp.get('valuation_zone'))}</b></div></div><table><tr><th>法人</th><th>前次 → 最新</th><th>EPS 修正</th><th>目標價修正</th><th>最新目標</th></tr>{erows}</table><p class='small muted'>{esc(exp.get('methodology'))}</p>
    <h2>3. Fundamentals & EPS Integrity</h2><table><tr><th>最新月營收</th><th>YoY</th><th>單季 EPS</th><th>YTD EPS</th><th>TTM EPS</th><th>財報期間</th></tr><tr><td>{nfmt(rev.get('latest_revenue'),0)}</td><td>{pct(rev.get('revenue_yoy'))}</td><td>{nfmt(d.get('eps_stack',{}).get('quarter_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ytd_eps'),2)}</td><td>{nfmt(d.get('eps_stack',{}).get('ttm_eps'),2)}</td><td>{esc(fin.get('period') or fin.get('statement_date'))}</td></tr></table><p class='small muted'>{esc(d.get('eps_stack',{}).get('note'))}</p>
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


@app.get("/api/diagnostics/financial/{ticker}")
async def financial_diagnostics(ticker: str):
    ticker=ticker.strip()
    if not ticker.isdigit() or len(ticker) not in (4,5,6): raise HTTPException(400,"請輸入有效台股代號")
    result=await diagnose_official_financial_sources(ticker)
    return JSONResponse(result, headers={"Cache-Control":"no-store"})

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
    out=REPORT_DIR/f"{ticker}_{stamp}_research_v5_2_4a.pdf"
    HTML(string=report_html(d),base_url=str(ROOT)).write_pdf(out)
    return FileResponse(out,media_type="application/pdf",filename=f"{ticker}_AI_research_V5_2_4a_{stamp}.pdf")

@app.post("/api/cache/clear")
async def cache_clear():
    _CACHE.clear(); return {"status":"ok","message":"cache cleared"}

@app.get("/health")
async def health(): return {"status":"ok","version":"5.2.7","mode":"cloud-mobile-official-eps-engine","finmind_token":bool(FINMIND_TOKEN),"cache_ttl_seconds":CACHE_TTL,"pwa":True}
