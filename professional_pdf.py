"""Professional multi-page Taiwan equity research PDF renderer."""
from __future__ import annotations
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

NAVY=colors.HexColor("#102A3A"); NAVY2=colors.HexColor("#173F50"); TEAL=colors.HexColor("#21B38B")
RED=colors.HexColor("#D95F6A"); GOLD=colors.HexColor("#D9A441"); INK=colors.HexColor("#1B2B34")
MUTED=colors.HexColor("#607782"); GRID=colors.HexColor("#D7E2E6"); PALE=colors.HexColor("#F2F7F7")

def _num(v):
    try:return float(v) if v is not None else None
    except (TypeError,ValueError):return None
def _fmt(v,d=1,s=""):
    n=_num(v); return "—" if n is None else f"{n:,.{d}f}{s}"
def _pct(v,d=1):
    n=_num(v); return "—" if n is None else f"{n:+,.{d}f}%"
def _money(v):
    n=_num(v)
    if n is None:return "—"
    sign="+" if n>0 else "" if n==0 else "-"; a=abs(n)
    return f"{sign}{a/100_000_000:,.2f} 億元" if a>=100_000_000 else f"{sign}{a/10_000:,.0f} 萬元" if a>=10_000 else f"{n:,.0f} 元"
def _revenue(v):
    n=_num(v); return "—" if n is None else f"{n/100_000_000:,.2f} 億元"
def _esc(v):return html.escape("—" if v is None else str(v))
def _human(v):
    return {"cumulative_ytd":"官方累計口徑","single_source":"單一有效來源","consensus":"多筆有效樣本","net_shares_x_daily_close":"每日淨買賣股數 × 當日收盤價估算","trend":"技術趨勢","positioning":"法人籌碼","valuation":"估值"}.get(str(v),str(v) if v is not None else "—")

def _risk_levels(t:dict[str,Any],price:float|None):
    if price is None:return None,None,None
    series=t.get("series") or []; last20=series[-20:]; last60=series[-60:]
    def lows(rows):return min((_num(r.get("low")) for r in rows if _num(r.get("low")) is not None),default=None)
    supports=sorted({x for x in (lows(last20),lows(last60),_num(t.get("support1")),_num(t.get("support2"))) if x is not None and x<=price},reverse=True)
    ma=t.get("ma") or {}
    resist=sorted({x for x in (_num(t.get("resistance")),_num(t.get("support1")),_num(t.get("support2")),_num(ma.get("20")),_num(ma.get("60"))) if x is not None and x>=price})
    s1=supports[0] if supports else price*.95; s2=next((x for x in supports[1:] if x<s1*.985),s1*.93)
    return s1,s2,resist[0] if resist else price*1.08

def _table(rows,font,widths=None,size=8,right=None):
    style=ParagraphStyle("cell",fontName=font,fontSize=size,leading=size+3,textColor=INK)
    header_style=ParagraphStyle("header-cell",parent=style,textColor=colors.white)
    wrapped=[[c if hasattr(c,"wrap") else Paragraph(_esc(c),header_style if row_index==0 else style) for c in row]
             for row_index,row in enumerate(rows)]
    t=Table(wrapped,colWidths=widths,repeatRows=1,hAlign="LEFT")
    cmds=[("FONTNAME",(0,0),(-1,-1),font),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),.35,GRID),("BACKGROUND",(0,0),(-1,0),NAVY2),("TEXTCOLOR",(0,0),(-1,0),colors.white),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,PALE]),("PADDING",(0,0),(-1,-1),5)]
    if right is not None:cmds.append(("ALIGN",(right,1),(-1,-1),"RIGHT"))
    t.setStyle(TableStyle(cmds)); return t

def _chart(rows,series,label_key,width=172*mm,height=55*mm,bar=None):
    d=Drawing(width,height); L,R,T,B=13*mm,6*mm,7*mm,10*mm; W,H=width-L-R,height-T-B
    vals=[_num(r.get(k)) for r in rows for k,_,_ in series]; vals=[x for x in vals if x is not None]
    if not rows or not vals:
        d.add(String(width/2,height/2,"資料不足，無法繪圖",fontName="NotoSansTC",fontSize=9,textAnchor="middle",fillColor=MUTED)); return d
    lo,hi=min(vals),max(vals); pad=max((hi-lo)*.12,abs(hi)*.03,1); lo-=pad; hi+=pad
    d.add(Rect(L,B,W,H,fillColor=colors.white,strokeColor=GRID,strokeWidth=.5))
    for i in range(4):
        y=B+H*i/3; d.add(Line(L,y,L+W,y,strokeColor=GRID,strokeWidth=.35)); d.add(String(L-2,y-2,f"{lo+(hi-lo)*i/3:,.0f}",fontName="NotoSansTC",fontSize=6.5,textAnchor="end",fillColor=MUTED))
    if bar:
        bv=[_num(r.get(bar)) or 0 for r in rows]; mx=max((abs(x) for x in bv),default=1) or 1; bw=max(1,W/max(len(rows),1)*.55)
        for i,v in enumerate(bv):
            x=L+W*i/max(len(rows)-1,1); d.add(Rect(x-bw/2,B,bw,H*.22*abs(v)/mx,fillColor=colors.HexColor("#C8D6DA"),strokeColor=None))
    for key,label,color in series:
        pts=[]
        for i,r in enumerate(rows):
            v=_num(r.get(key))
            if v is not None:pts.append((L+W*i/max(len(rows)-1,1),B+H*(v-lo)/(hi-lo)))
        for a,b in zip(pts,pts[1:]):d.add(Line(a[0],a[1],b[0],b[1],strokeColor=color,strokeWidth=1.25))
    for i in sorted({0,max(0,len(rows)//2),len(rows)-1}):
        d.add(String(L+W*i/max(len(rows)-1,1),2,str(rows[i].get(label_key) or "")[:10],fontName="NotoSansTC",fontSize=6.5,textAnchor="middle",fillColor=MUTED))
    x=L
    for _,label,color in series:
        d.add(Line(x,height-3,x+10,height-3,strokeColor=color,strokeWidth=2)); d.add(String(x+13,height-5,label,fontName="NotoSansTC",fontSize=7,fillColor=INK)); x+=58
    return d

def write_professional_pdf(data:dict[str,Any],out:Path,root:Path,version:str):
    font="NotoSansTC"; fp=root/"assets/fonts/NotoSansTC-VF.ttf"
    if not fp.is_file():raise FileNotFoundError("bundled Traditional Chinese PDF font is missing")
    if font not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(font,str(fp)))
    base=getSampleStyleSheet(); title=ParagraphStyle("title",parent=base["Title"],fontName=font,fontSize=28,leading=34,textColor=NAVY,alignment=TA_LEFT)
    sub=ParagraphStyle("sub",fontName=font,fontSize=10,leading=16,textColor=MUTED); h1=ParagraphStyle("h1",fontName=font,fontSize=17,leading=22,textColor=NAVY,spaceAfter=8)
    h2=ParagraphStyle("h2",fontName=font,fontSize=12,leading=17,textColor=NAVY2,spaceBefore=7,spaceAfter=5); body=ParagraphStyle("body",fontName=font,fontSize=9,leading=14.5,textColor=INK)
    small=ParagraphStyle("small",fontName=font,fontSize=7.5,leading=11,textColor=MUTED); call=ParagraphStyle("call",parent=body,backColor=PALE,borderColor=TEAL,borderWidth=1,borderPadding=9,spaceAfter=7)
    warn=ParagraphStyle("warn",parent=body,backColor=colors.HexColor("#FFF5E1"),borderColor=GOLD,borderWidth=1,borderPadding=8)
    ticker=str(data.get("ticker") or ""); name=str(data.get("name") or ticker); price=_num(data.get("price")); generated=str(data.get("generated_at") or datetime.now().astimezone().isoformat())
    sc=data.get("scores") or {}; fin=data.get("financial") or {}; eps=data.get("eps_stack") or {}; rev=data.get("revenue") or {}; tech=data.get("technical") or {}; flow=data.get("flow") or {}; val=data.get("valuation") or {}; research=data.get("research") or {}; pipe=data.get("research_pipeline") or {}; conf=data.get("confidence") or {}; per=data.get("per") or {}
    doc=SimpleDocTemplate(str(out),pagesize=A4,rightMargin=16*mm,leftMargin=16*mm,topMargin=16*mm,bottomMargin=16*mm,title=f"{name} {ticker} 專業投資研究報告 V{version}",author="AI Stock Research Terminal")
    def frame(c,d):
        c.saveState(); c.setStrokeColor(NAVY); c.line(16*mm,12*mm,A4[0]-16*mm,12*mm); c.setFont(font,7); c.setFillColor(MUTED); c.drawString(16*mm,7.5*mm,f"{name} {ticker}｜AI Stock Research Terminal V{version}"); c.drawRightString(A4[0]-16*mm,7.5*mm,f"第 {d.page} 頁"); c.restoreState()
    story=[Spacer(1,8*mm),Paragraph("TAIWAN EQUITY RESEARCH",sub),Paragraph(f"{_esc(name)} <font color='#607782'>{_esc(ticker)}</font>",title),Paragraph(f"專業投資研究報告｜資料截至 {_esc(tech.get('last_date') or generated[:10])}｜產生時間 {_esc(generated)}",sub),Spacer(1,8*mm)]
    metrics=[["最新收盤","研究立場","綜合評分","可信度"],[_fmt(price,1," 元"),data.get("stance"),f"{sc.get('綜合','—')}/100",f"{conf.get('overall','—')}/100"]]
    story += [_table(metrics,font,[42*mm]*4),Spacer(1,8*mm),Paragraph("投資結論",h1),Paragraph(_esc(data.get("thesis") or pipe.get("investment_view") or "資料不足，暫不形成結論。"),call)]
    cats=data.get("catalysts") or pipe.get("catalysts") or []; risks=data.get("risks") or pipe.get("risks") or []
    story += [_table([["核心催化劑","核心風險"],["<br/>".join(f"• {_esc(x)}" for x in cats[:4]) or "—","<br/>".join(f"• {_esc(x)}" for x in risks[:4]) or "—"]],font,[84*mm]*2),Spacer(1,5*mm),Paragraph("閱讀原則：模型合理價、法人目標價與市場價格分開呈現。歷史均值回歸價不直接視為短期目標價。",warn),PageBreak()]
    # Fundamentals
    rows=[["指標","最新值","期間 / 口徑","資料屬性"],["最新月營收",_revenue(rev.get("latest_revenue")),rev.get("revenue_period") or rev.get("last_date"),"公司公告實際值"],["月營收年增",_pct(rev.get("revenue_yoy")),"最新月","衍生計算"],["近3月營收年增",_pct(rev.get("revenue_3m_yoy")),"近3月","衍生計算"],["單季 EPS",_fmt(eps.get("quarter_eps"),2),fin.get("period"),"官方 / 期間閘門"],["YTD EPS",_fmt(eps.get("ytd_eps"),2),fin.get("period"),"官方累計值"],["TTM EPS",_fmt(eps.get("ttm_eps"),2),val.get("eps_basis"),"衍生計算"],["毛利率",_pct(fin.get("gross_margin")),fin.get("margin_period"),_human(fin.get("margin_basis"))],["營益率",_pct(fin.get("operating_margin")),fin.get("margin_period"),_human(fin.get("margin_basis"))]]
    seen=set(); rr=[]
    for r in rev.get("series") or []:
        p=r.get("period") or r.get("date")
        if p and p not in seen:seen.add(p); rr.append({"period":p,"value":(_num(r.get("revenue")) or 0)/100_000_000})
    story += [Paragraph("02｜基本面與盈餘品質",h1),_table(rows,font,[38*mm,35*mm,62*mm,35*mm]),Paragraph("近24月公司公告實際營收（億元）",h2),_chart(rr[-24:],[("value","月營收（億元）",TEAL)],"period"),Paragraph("EPS規則："+_esc(str(eps.get("note") or fin.get("margin_warning") or "官方財報期間已通過完整性檢查。").replace("V5.17.4：","")),small),PageBreak()]
    # Positioning
    pos=[["參與者","1日估算金額","5日估算金額","20日估算金額"]]+[[label,_money(flow.get(f"{p}_1_amount")),_money(flow.get(f"{p}_5_amount")),_money(flow.get(f"{p}_20_amount"))] for label,p in (("外資","foreign"),("投信","trust"),("自營商","dealer"))]
    marg=flow.get("margin_history") or []
    story += [Paragraph("03｜法人籌碼與融資融券",h1),_table(pos,font,[32*mm,46*mm,46*mm,46*mm],right=1),Paragraph(_esc(_human(flow.get("institutional_amount_method")))+"；非官方逐筆成交金額。",small),Paragraph("融資融券餘額趨勢（官方張數）",h2),_chart(marg[-60:],[("margin_balance","融資餘額",GOLD),("short_balance","融券餘額",RED)],"date"),_table([["指標","今日","5日","20日","截至"],["融資餘額變化",_pct(flow.get("margin_1_pct")),_pct(flow.get("margin_5_pct")),_pct(flow.get("margin_20_pct")),flow.get("margin_last_date")],["融券餘額變化",_pct(flow.get("short_1_pct")),_pct(flow.get("short_5_pct")),_pct(flow.get("short_20_pct")),flow.get("margin_last_date")],["券資比",_pct(flow.get("short_margin_ratio_pct")),"—","—",flow.get("margin_last_date")]],font,[42*mm,31*mm,31*mm,31*mm,35*mm]),PageBreak()]
    # Technical
    s1,s2,res=_risk_levels(tech,price)
    story += [Paragraph("04｜價格趨勢與技術風險",h1),_chart((tech.get("series") or [])[-252:],[("close","收盤",NAVY2),("ma20","MA20",GOLD),("ma60","MA60",TEAL)],"date",bar="volume"),_table([["技術指標","數值","解讀基準","目前狀態"],["趨勢",tech.get("trend"),"價格、MA20、MA60綜合",tech.get("chart_period")],["RSI14",_fmt(tech.get("rsi14"),1),">70過熱；<30超賣","動能參考"],["KD",f"K {_fmt(tech.get('k'),1)} / D {_fmt(tech.get('d'),1)}","交叉與極端區","短線動能"],["MACD Hist",_fmt(tech.get("macd_hist"),2),"正負值與轉折","趨勢動能"],["20日報酬",_pct(tech.get("return_20d")),"收盤價變化","短線"],["60日報酬",_pct(tech.get("return_60d")),"收盤價變化","波段"]],font,[38*mm,42*mm,48*mm,42*mm]),Paragraph("關鍵價位與觀察條件",h2),_table([["價位","數值","研究用途"],["第一支撐",_fmt(s1,1," 元"),"近20日低點與價格結構"],["第二支撐",_fmt(s2,1," 元"),"近60日低點與中期防守"],["第一壓力",_fmt(res,1," 元"),"高於現價的近期均線或壓力"],["52週高 / 低",f"{_fmt(tech.get('high_52w'),1)} / {_fmt(tech.get('low_52w'),1)}","波動區間"]],font,[42*mm,45*mm,83*mm]),PageBreak()]
    # Valuation
    mi=val.get("market_implied") or {}; an=val.get("analyst_consensus") or {}; base_target=_num(next((x.get("target") for x in val.get("scenarios",[]) if x.get("name")=="基準"),None)); fwd_target=_num(next((x.get("target") for x in val.get("forward_scenarios",[]) if x.get("name")=="基準"),None))
    rec=[["估值視角","EPS基礎","倍數 / 方法","價格","與現價關係"],["市場現況",_fmt(mi.get("ttm_eps"),2),f"隱含 PE {_fmt(mi.get('implied_pe'),1)}x",_fmt(price,0),"現價"],["歷史均值回歸",_fmt(val.get("anchor_eps"),2),val.get("pe_basis"),_fmt(base_target,0),"非短期目標價"],["Forward模型",_fmt(research.get("median_forward_eps"),2),f"{research.get('forward_eps_year') or 'Forward'} EPS",_fmt(fwd_target,0),_human(val.get("forward_status"))],["公開法人目標價中位數","—",f"覆蓋 {an.get('coverage') or research.get('target_coverage') or 0} 筆",_fmt(an.get("median_target") or research.get("median_target"),0),"公開引用，非官方事實"]]
    scen=[["情境","EPS","合理PE","模型價格","相對現價"]]+[[x.get("name"),_fmt(x.get("eps"),2),_fmt(x.get("pe"),1,"x"),_fmt(x.get("target"),0),_pct(x.get("upside_pct"))] for x in val.get("scenarios") or []]
    ev=sorted({_num(x.get("eps")) for x in val.get("forward_scenarios") or [] if _num(x.get("eps")) is not None}) or [(_num(val.get("anchor_eps")) or 0)*q for q in (.9,1,1.1)]; pv=sorted({_num(x.get("pe")) for x in val.get("scenarios") or [] if _num(x.get("pe")) is not None}) or [15,20,25]
    sens=[["Forward EPS / PE"]+[_fmt(p,1,"x") for p in pv]]+[[_fmt(e,2)]+[_fmt(e*p,0) for p in pv] for e in ev]
    story += [Paragraph("05｜估值、法人預期與市場隱含假設",h1),_table(rec,font,[39*mm,31*mm,48*mm,27*mm,32*mm]),Spacer(1,3*mm),Paragraph(_esc(val.get("valuation_warning") or "估值模型對EPS與倍數高度敏感。"),warn),Paragraph("情境估值",h2),_table(scen,font,[32*mm,34*mm,34*mm,36*mm,34*mm],right=1),Paragraph("EPS × PE 敏感度矩陣（元）",h2),_table(sens,font,[44*mm]+[42*mm]*len(pv),right=1),Paragraph(f"歷史PER分位：P25 {_fmt(per.get('pe_p25'),1)}x｜中位數 {_fmt(per.get('pe_median'),1)}x｜P75 {_fmt(per.get('pe_p75'),1)}x｜樣本 {per.get('sample_count','—')} 日。",small),PageBreak()]
    # Public research
    reports=[["機構 / 出處","日期","評等","目標價","Forward EPS","信心"]]+[[x.get("institution") or x.get("publisher"),x.get("report_date"),x.get("rating"),_fmt(x.get("target_price"),0),_fmt(x.get("forward_eps"),2),x.get("confidence")] for x in (research.get("reports") or [])[:12]]
    if len(reports)==1:reports.append(["目前無可解析公開引用","—","—","—","—","—"])
    story += [Paragraph("06｜公開法人研究與預期修正",h1),Paragraph(f"可解析公開研究引用 {research.get('count',0)} 筆；目標價有效樣本 {research.get('target_coverage',0)} 筆；Forward EPS有效樣本 {research.get('eps_coverage',0)} 筆。公開媒體轉述可能存在雜訊，只作市場預期參考。",call),_table(reports,font,[39*mm,26*mm,25*mm,29*mm,31*mm,20*mm],size=7.4,right=3),Spacer(1,4*mm),Paragraph("目標價中位數不等於本系統合理價。法人可能使用更遠年度EPS、SOTP或成長溢價；歷史PER模型偏向均值回歸。",warn),PageBreak()]
    # Risk plan
    ma60=_num((tech.get("ma") or {}).get("60")); status={"trend":"已滿足" if price is not None and ma60 is not None and price>=ma60 else "尚未滿足","positioning":"已滿足" if (_num(flow.get("foreign_20_amount")) or 0)>0 else "尚未滿足","valuation":"已滿足" if price is not None and base_target is not None and price<=base_target else "尚未滿足"}
    cond=[["類型","可觀察條件","目前狀態","意義"]]+[[_human(x.get("type")),x.get("condition"),status.get(str(x.get("type")),"待觀察"),x.get("meaning")] for x in pipe.get("action_conditions") or []]
    riskrows=[["層級","參考價位","執行紀律","依據"],["觀察 / 進場確認",_fmt(s1,1," 元"),"等收盤確認與量價止穩，不追逐盤中尖峰","第一支撐"],["風險降低 1",_fmt(s1*.97 if s1 else None,1," 元"),"跌破且無法站回時先降低曝險","支撐下方約3%"],["風險降低 2",_fmt(s2,1," 元"),"中期支撐失守時進一步降低部位","第二支撐"],["論點失效",_fmt(s2*.95 if s2 else None,1," 元"),"基本面與技術面同步惡化時停止原假設","第二支撐下方約5%"],["第一獲利觀察",_fmt(res,1," 元"),"遇壓可分批鎖定，不預設必然突破","近期壓力"],["突破後管理",_fmt(res*1.08 if res else None,1," 元"),"只在放量突破並站穩後採移動停利","壓力上方約8%"]]
    story += [Paragraph("07｜投資條件、失效條件與風險管理",h1),Paragraph(_esc(pipe.get("investment_view") or data.get("thesis")),call),Paragraph("研究成立條件",h2),_table(cond,font,[28*mm,72*mm,28*mm,42*mm]),Paragraph("論點失效條件",h2),Paragraph("<br/>".join(f"• {_esc(x)}" for x in pipe.get("invalidation_conditions") or []) or "—",body),Paragraph("分層風險管理範例",h2),_table(riskrows,font,[33*mm,35*mm,68*mm,34*mm],size=7.6),Spacer(1,3*mm),Paragraph("以上為研究與風險管理框架，不是個人化投資建議。",warn),PageBreak()]
    # Sources
    sources=[["資料層","Dataset / 來源","截至","狀態"]]+[[x.get("name"),x.get("dataset"),x.get("as_of"),"OK" if x.get("status")=="ok" else "缺資料"] for x in data.get("source_status") or []]; boundary=pipe.get("data_boundary") or {}
    story += [Paragraph("08｜資料來源、品質邊界與重要揭露",h1),_table(sources,font,[40*mm,80*mm,32*mm,18*mm],size=7.2),Paragraph("資料治理原則",h2),Paragraph(_esc(data.get("data_policy") or pipe.get("policy")),body),Paragraph(f"Data Boundary Grade：{_esc(boundary.get('grade'))}",h2),Paragraph(_esc(boundary.get("message")),call),Paragraph("方法與限制",h2),Paragraph("本報告以TWSE、TPEx、MOPS及公司公告為主要事實來源；Yahoo Finance、FinMind及公開網路引用只在官方資料不足時作備援或交叉驗證。法人買賣金額為淨買賣股數乘當日收盤價的估算值。",body),Spacer(1,5*mm),Paragraph("重要聲明：本系統是研究與資訊整理工具，不構成個人化投資建議、證券招攬、收益保證或對任何價格的承諾。",warn)]
    doc.build(story,onFirstPage=frame,onLaterPages=frame)
