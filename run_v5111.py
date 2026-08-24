"""V5.11.1 hotfix: force frontend/API identity to current runtime."""
from __future__ import annotations
import re
from fastapi.responses import HTMLResponse, JSONResponse
import run_v5110, server

VERSION='5.11.1'
app=server.app
server.app.version=VERSION

for r in list(app.routes):
    if getattr(r,'path',None) in ('/','/health','/api/stock/{ticker}'):
        try: app.routes.remove(r)
        except ValueError: pass

@app.get('/api/stock/{ticker}')
async def api_stock_v5111(ticker:str, refresh:int=0):
    d=await run_v5110.build_stock_v5110(ticker,bool(refresh))
    if isinstance(d,dict):
        d['version']=VERSION
        # Never display ticker as company name. A missing identity remains explicit.
        name=str(d.get('name') or d.get('company_name') or '').strip()
        if not name or name==ticker or name==f'{ticker} {ticker}':
            known={'2330':'台積電','2454':'聯發科','3661':'世芯-KY','3665':'貿聯-KY'}
            name=known.get(ticker,'公司名稱待官方資料')
        d['name']=name
        d['company_name']=name
        d.setdefault('pipeline_diagnostics',{})['frontend_v5111']={'status':'ok','version':VERSION}
    return JSONResponse(d,headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0','X-AI-Stock-Version':VERSION})

@app.get('/health')
async def health_v5111():
    return JSONResponse({'status':'ok','version':VERSION,'mode':'frontend-api-identity-hotfix'},headers={'Cache-Control':'no-store','X-AI-Stock-Version':VERSION})

@app.get('/',response_class=HTMLResponse)
async def root_v5111():
    text=(server.ROOT/'index.html').read_text(encoding='utf-8')
    # Remove all legacy runtime scripts and hard-coded version labels.
    text=re.sub(r'<script[^>]+src="(?:recovery|v\d+_hotfix)\.js[^\"]*"[^>]*></script>','',text,flags=re.I)
    text=re.sub(r'<script[^>]+src="app\.js[^\"]*"[^>]*></script>','',text,flags=re.I)
    text=re.sub(r'<script>if\(\'serviceWorker\'.*?</script>','',text,flags=re.S|re.I)
    text=re.sub(r'V5\.9\.3',f'V{VERSION}',text)
    text=re.sub(r'href="styles\.css(?:\?v=[^"]+)?"',f'href="/static/styles.css?v={VERSION}"',text)
    text=re.sub(r'href="/static/manifest\.webmanifest(?:\?v=[^"]+)?"',f'href="/static/manifest.webmanifest?v={VERSION}"',text)
    boot=f'''\n<script>window.AI_STOCK_VERSION="{VERSION}";</script>\n<script src="/static/app.js?v={VERSION}"></script>\n<script>\nwindow.addEventListener('DOMContentLoaded',()=>{{document.querySelectorAll('[data-app-version]').forEach(x=>x.textContent='V{VERSION}')}});\nif('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js?v={VERSION}',{{updateViaCache:'none'}}).then(r=>r.update()).catch(()=>{{}});}}\n</script>\n'''
    text=text.replace('</body>',boot+'</body>')
    return HTMLResponse(text,headers={'Cache-Control':'no-store, no-cache, must-revalidate, max-age=0','Pragma':'no-cache','Expires':'0','X-AI-Stock-Version':VERSION})

@app.middleware('http')
async def runtime_v5111(request,call_next):
    resp=await call_next(request)
    resp.headers['X-AI-Stock-Version']=VERSION
    return resp
