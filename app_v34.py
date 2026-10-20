"""
TURTLE V3.4 FIXED - 2MIN SCAN + HEALTH FOR UPTIMEROBOT
"""
import sys, os
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
print(f"[BOOT] V3.4 FIXED ROOT={ROOT_DIR}")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import traceback, json, numpy as np, time
from datetime import datetime
from zoneinfo import ZoneInfo
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler
import requests

try:
    from core.donatello.persistent_state import load_state, save_state, log_day, DEFAULT_STATE
    from core.leonardo.reasoning import get_market_data, check_filters, STABLECOIN_BLACKLIST, get_ohlc_three_windows, calc_atr, calc_bollinger_width, calc_rsi
    from core.raphael.execution import open_position, monitor_positions, wizdraw_manual_close
    from core.splinter.turtle_scanner import TurtleScanner
    from core.splinter.cell import SplinterCell
    from core.moneyflow.scanner import MoneyFlowScanner
    from core.exchanges.connector import connector
    print(f"[BOOT] core OK - {list(connector.exchanges.keys())}")
except Exception as e:
    print(f"[BOOT] core fail {e}")
    raise

try:
    from paper_trading_execution_V3_4 import PaperTradingExecutionV34
except:
    class PaperTradingExecutionV34:
        def __init__(self):
            self.budget=100000; self.fee_pct=0.0004; self.slippage_pct=0.0002; self.config={"max_exposure_usd":30000}; self.logs=[]
        def get_leverage_for_score(self,s,b1,b4,u,f): return (4,"KURZ") if s>=10 else (3,"MITTEL") if s>=9 else (2,"MITTEL")
        def calculate_position_size(self,lev,score): return 3*lev, 100000*3*lev/100
        def get_time_stop_hours(self,mode,lev): return {"KURZ":4,"MITTEL":24,"LANG":168}.get(mode,24)/(lev/2)
        def should_auto_sell(self,pos): return False,"HOLD",{}
        def evaluate_paper_distribution(self,trades): return {}

app = FastAPI(title="TURTLE V3.4 FIXED")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MAX_TRADES_PER_DAY=2; COOLDOWN_AFTER_TRADE_MIN=30; COOLDOWN_AFTER_LOSS_MIN=120
DAILY_PROFIT_TARGET_PCT=8.0; DAILY_LOSS_LIMIT_PCT=-4.0
BLACKLIST_START=22; BLACKLIST_END=8; BEST_START=9; BEST_END=15
LOCK=Lock(); STATE=load_state(); BOOT_TIME=datetime.now(ZoneInfo("Europe/Berlin"))

def load_universe_100():
    try:
        url="https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=volume_desc&per_page=100&page=1"
        r=requests.get(url, timeout=15); j=r.json()
        if isinstance(j,list):
            uni=[f"{d['symbol'].upper()}USDT" for d in j if d.get('symbol') and d['symbol'].upper() not in STABLECOIN_BLACKLIST][:100]
            if len(uni)>=30: return uni
    except Exception as e: print(f"Universe fail {e}")
    fallback=["WIFUSDT","PEPEUSDT","BONKUSDT","FLOKIUSDT","POPCATUSDT","SHIBUSDT","DOGEUSDT","HYPEUSDT","SOLUSDT","AVAXUSDT","LINKUSDT","ARBUSDT","OPUSDT","APTUSDT","SUIUSDT","TIAUSDT","SEIUSDT","WUSDT","ENAUSDT","PENDLEUSDT","ONDOUSDT","RENDERUSDT","FETUSDT","TAOUSDT","WLDUSDT"]
    while len(fallback)<100: fallback.extend(fallback[:20])
    return fallback[:100]

UNIVERSE_100=load_universe_100()
turtle_scanner=TurtleScanner(UNIVERSE_100)
splinter_cell=SplinterCell()
moneyflow_scanner=MoneyFlowScanner()
paper_executor=PaperTradingExecutionV34()

def json_safe(obj):
    if isinstance(obj,(np.bool_,bool)): return bool(obj)
    if isinstance(obj,(np.integer,)): return int(obj)
    if isinstance(obj,(np.floating,)): return float(obj)
    if isinstance(obj,np.ndarray): return obj.tolist()
    if isinstance(obj,dict): return {k:json_safe(v) for k,v in obj.items()}
    if isinstance(obj,(list,tuple)): return [json_safe(x) for x in obj]
    if hasattr(obj,'__dict__'): return json_safe(obj.__dict__)
    return obj

def get_current_whitelist():
    dyn=STATE.get("dynamic_whitelist")
    if dyn and len(dyn)>=5:
        filtered=[s for s in dyn if s[:-4] not in STABLECOIN_BLACKLIST]
        if len(filtered)>=5: return filtered
        return filtered + [x for x in UNIVERSE_100 if x not in filtered][:8-len(filtered)]
    return UNIVERSE_100[:20]

class PoolPayload(BaseModel):
    pool:List[str]; details:Optional[List[dict]]=None

def is_blacklist_time():
    h=datetime.now(ZoneInfo("Europe/Berlin")).hour
    if h>=BLACKLIST_START or h<BLACKLIST_END: return True,f"BLACKLIST {h}:00 CET"
    return False,""

def is_best_time():
    return BEST_START <= datetime.now(ZoneInfo("Europe/Berlin")).hour < BEST_END

def check_daily_reset():
    global STATE
    today=datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    if STATE["last_reset_date"]!=today:
        try: log_day({"date":STATE["last_reset_date"],"trades":STATE["trades_today"],"pnl_pct":STATE["daily_pnl_pct"],"equity_end":STATE["equity"]})
        except: pass
        STATE["initial_equity_today"]=STATE["equity"]; STATE["daily_pnl_pct"]=0; STATE["daily_pnl_usd"]=0; STATE["trades_today"]=0; STATE["closed_today"]=[]; STATE["status"]="JAGT"; STATE["status_reason"]="Neuer Tag"; STATE["last_reset_date"]=today; save_state(STATE)

def update_status():
    global STATE
    try:
        check_daily_reset()
        if STATE["daily_pnl_pct"]>=DAILY_PROFIT_TARGET_PCT: STATE["status"]="FEIERABEND_GEWINN"; STATE["status_reason"]=f"Target +{DAILY_PROFIT_TARGET_PCT}%"; return
        if STATE["daily_pnl_pct"]<=DAILY_LOSS_LIMIT_PCT: STATE["status"]="FEIERABEND_SCHUTZ"; STATE["status_reason"]=f"Loss {DAILY_LOSS_LIMIT_PCT}%"; return
        if STATE["trades_today"]>=MAX_TRADES_PER_DAY: STATE["status"]="FEIERABEND_GEWINN"; STATE["status_reason"]=f"Max {MAX_TRADES_PER_DAY} Trades"; return
        now=datetime.now(ZoneInfo("Europe/Berlin"))
        if STATE["last_loss_time"]:
            try:
                last_loss=datetime.fromisoformat(STATE["last_loss_time"])
                if (now-last_loss).total_seconds()<COOLDOWN_AFTER_LOSS_MIN*60: STATE["status"]="PAUSE"; STATE["status_reason"]=f"Anti-Revenge"; return
            except: pass
        if STATE["last_trade_end"]:
            try:
                last_end=datetime.fromisoformat(STATE["last_trade_end"])
                if (now-last_end).total_seconds()<COOLDOWN_AFTER_TRADE_MIN*60: STATE["status"]="PAUSE"; STATE["status_reason"]=f"Cooldown"; return
            except: pass
        is_black,reason=is_blacklist_time()
        if is_black: STATE["status"]="PAUSE"; STATE["status_reason"]=reason; return
        STATE["status"]="JAGT"; STATE["status_reason"]="Beste Zeit" if is_best_time() else "Jagt vorsichtig"
    except Exception as e: STATE["status"]="JAGT"; STATE["status_reason"]=f"Fail {e}"

def paper_monitor_positions():
    with LOCK:
        try:
            if not STATE.get("open_positions"): return
            for pos in STATE.get("open_positions",[])[:]:
                try:
                    data=get_market_data(pos["symbol"]); curr=float(data.get("price",pos["current_price"]))
                    if curr==0: continue
                    try: entry_dt=datetime.fromisoformat(pos["entry_time"]); hours_held=(datetime.now(ZoneInfo("Europe/Berlin"))-entry_dt).total_seconds()/3600
                    except: hours_held=0
                    pnl_pct=(pos["entry"]-curr)/pos["entry"]*100*pos.get("leverage",2)
                    paper_pos={"symbol":pos["symbol"],"pnl_percent":pnl_pct,"pnl_usd":pos.get("position_usd",6000)*pnl_pct/100,"hold_mode":pos.get("hold_mode","MITTEL"),"hours_held":hours_held,"leverage":pos.get("leverage",2),"be_set":pos.get("be_triggered",False),"trend_bed1_ok":True,"moneyflow_bed4_ok":True,"rsi_1h":50,"vol_spike":0,"bb_expansion_up":False}
                    should_sell,reason,meta=paper_executor.should_auto_sell(paper_pos)
                    if should_sell:
                        from core.raphael.execution import close_position
                        close_position(pos,curr,f"PAPER {reason}",STATE)
                except Exception as e: print(f"paper_monitor fail {e}")
            save_state(STATE)
        except Exception as e: print(f"paper_monitor error {e}")

def run_turtle_scan():
    start=time.time()
    with LOCK:
        try:
            print(f"[Turtle] Scan 100 Coins - Start {datetime.now().isoformat()} - dauert 30-40s")
            queue=turtle_scanner.scan_once(get_market_data)
            STATE["vorauswahl_queue"]=queue[:20]
            save_state(STATE)
            print(f"[Turtle] Fertig in {time.time()-start:.1f}s - Top: {[q['symbol'] for q in queue[:5]]}")
            return queue
        except Exception as e:
            print(f"Turtle fail {e} nach {time.time()-start:.1f}s")
            traceback.print_exc()
            return []

def run_splinter_inspection():
    with LOCK:
        try:
            top8=turtle_scanner.get_top_for_splinter(8)
            if not top8: print("[Splinter] Keine Coins in Queue"); return []
            print(f"[Splinter] Besichtigung {len(top8)} Coins")
            inspection_results=[]
            for item in top8:
                sym=item["symbol"] if isinstance(item,dict) else item
                try:
                    ohlc_24h,ohlc_30d,ohlc_180d=get_ohlc_three_windows(sym)
                    if ohlc_24h is None: continue
                    atr=calc_atr(ohlc_24h); bb_width=calc_bollinger_width(ohlc_24h); rsi=calc_rsi(ohlc_24h['close']) if ohlc_24h is not None else 50
                    vol_24h_pct=item.get("change",0) if isinstance(item,dict) else 0
                    mf_signal=moneyflow_scanner.scan(sym,vol_24h_pct)
                    STATE["money_flow"]=json_safe(mf_signal.meta)
                    decision=splinter_cell.inspect(symbol=sym, ohlc_24h=ohlc_24h, ohlc_30d=ohlc_30d, ohlc_180d=ohlc_180d, vol_24h_pct=vol_24h_pct, atr=atr, bb_width=bb_width, moneyflow_ok=mf_signal.ok_for_short, moneyflow_meta=mf_signal.meta)
                    inspection_results.append(json_safe(decision))
                    print(f"[Splinter] {sym} Bed1:{decision.passed_bed1} Bed4:{decision.passed_bed4} Ready:{decision.ready_for_entry}")
                    if decision.ready_for_entry and len(STATE.get("open_positions",[]))==0 and STATE["status"]=="JAGT":
                        data=get_market_data(sym)
                        if data["price"]==0: data["price"]=item.get("price",0) if isinstance(item,dict) else 0
                        if data["price"]>0:
                            meta={"atr":atr,"bb_bw":bb_width,"vol_ratio":item.get("vol_ratio",1.5) if isinstance(item,dict) else 1.5,"rsi":rsi}
                            score=int(item.get("score",7)) if isinstance(item,dict) else 7
                            bed1_perfect=(decision.bed1_meta.get("ath_dd",-15)<=-15 and decision.bed1_meta.get("ath_dd",0)>=-25)
                            bed4_perfect=(mf_signal.verdict=="GUT")
                            usdt_dom_up=mf_signal.meta.get("usdt_dom_change",0)>0.5
                            funding_neg=mf_signal.funding_rate<-0.02
                            leverage,hold_mode_paper=paper_executor.get_leverage_for_score(score,bed1_perfect,bed4_perfect,usdt_dom_up,funding_neg)
                            if decision.hold_mode=="LANG" and leverage>2: leverage=2
                            if leverage>=4: hold_mode_paper="KURZ"
                            if leverage>=3: decision.hold_mode=hold_mode_paper
                            pos_pct,pos_usd=paper_executor.calculate_position_size(leverage,score)
                            current_exposure=sum([p.get("position_usd",6000) for p in STATE.get("open_positions",[])])
                            if current_exposure+pos_usd<=30000:
                                open_position(sym,data,meta,STATE,splinter_decision=decision)
                                if STATE.get("open_positions"):
                                    STATE["open_positions"][-1]["leverage"]=leverage; STATE["open_positions"][-1]["position_usd"]=pos_usd; STATE["open_positions"][-1]["time_stop_hours"]=paper_executor.get_time_stop_hours(decision.hold_mode,leverage)
                                save_state(STATE)
                                print(f"ENTRY {sym} Lev {leverage}x Hold {decision.hold_mode}")
                except Exception as e:
                    print(f"[Splinter] {sym} fail {e}")
                    continue
                time.sleep(0.5)
            STATE["splinter_inspection"]=inspection_results
            save_state(STATE)
            return inspection_results
        except Exception as e:
            traceback.print_exc()
            return []

def scan_all():
    global STATE
    try:
        update_status()
        whitelist=get_current_whitelist()
        if STATE["status"]!="JAGT": return json_safe({"status":STATE["status"],"reason":STATE["status_reason"],"results":[],"whitelist":whitelist})
        open_syms=[p["symbol"] for p in STATE.get("open_positions",[])]
        results=[]
        for sym in whitelist[:20]:
            try:
                data=get_market_data(sym)
                if data["price"]==0: continue
                ok,checks,meta=check_filters(sym,data,open_syms)
                results.append(json_safe({"symbol":sym,"ok":bool(ok),"checks":checks,"meta":meta,"price":float(data.get("price",0)),"change_24h":float(data.get("price_change_24h",0)),"source":str(data.get("source","none"))}))
            except: results.append(json_safe({"symbol":sym,"ok":False,"price":0}))
            time.sleep(0.2)
        return json_safe({"status":STATE["status"],"reason":STATE["status_reason"],"results":results,"whitelist":whitelist})
    except Exception as e:
        traceback.print_exc()
        return json_safe({"status":"ERROR","reason":str(e),"results":[],"whitelist":get_current_whitelist()})

# === HEALTH FÜR UPTIMEROBOT - SUPER SCHNELL OHNE LOCK ===
@app.get("/api/v34/health")
def health():
    uptime=(datetime.now(ZoneInfo("Europe/Berlin"))-BOOT_TIME).total_seconds()
    return {"status":"online","version":"V3.4 FIXED 2MIN","utc":datetime.utcnow().isoformat(),"uptime_sec":int(uptime),"exchanges":list(connector.exchanges.keys()),"universe":len(UNIVERSE_100),"queue":len(STATE.get("vorauswahl_queue",[])),"open":len(STATE.get("open_positions",[]))}

@app.get("/health")
def health_alias(): return {"status":"online","uptime":int((datetime.now(ZoneInfo("Europe/Berlin"))-BOOT_TIME).total_seconds())}

@app.get("/ping")
def ping(): return {"status":"online","ping":"pong","ts":datetime.utcnow().isoformat()}

@app.get("/", response_class=HTMLResponse)
def root_dashboard():
    # Dashboard direkt auf / - wichtig für Render Root URL
    try:
        tmpl_path = os.path.join(os.path.dirname(__file__), "templates/dashboard_v34.html")
        if os.path.exists(tmpl_path):
            with open(tmpl_path, "r", encoding="utf-8") as f:
                html = f.read()
            return HTMLResponse(html)
    except Exception as e:
        print(f"Dashboard template fail {e}")
    # Fallback wenn Template fehlt - trotzdem HTML liefern
    return HTMLResponse(f"""
    <html><head><title>TURTLE V3.4</title>
    <style>body{{background:#0f0f0f;color:#39ff14;font-family:monospace;padding:20px}} .card{{border:1px solid #39ff14;padding:15px;margin:10px 0;border-radius:10px}} a{{color:#39ff14}}</style>
    </head><body>
    <h1>🐢 TURTLE V3.4 SPLINTERZELLE - RUNNING</h1>
    <div class="card">Status: {STATE.get('status')} - {STATE.get('status_reason')}<br>
    Equity: ${STATE.get('equity',0):.2f} | Trades: {STATE.get('trades_today',0)}/2<br>
    Uptime: {int((datetime.now(ZoneInfo("Europe/Berlin"))-BOOT_TIME).total_seconds())}s</div>
    <div class="card"><b>API Endpoints:</b><br>
    <a href="/api/v34/health">/api/v34/health</a> - Health für UptimeRobot<br>
    <a href="/api/v34/turtles">/api/v34/turtles</a> - Turtle Queue<br>
    <a href="/api/v34/splinter">/api/v34/splinter</a> - Splinter Inspection<br>
    <a href="/api/v34/active">/api/v34/active</a> - Active Calls<br>
    <a href="/api/v34/moneyflow">/api/v34/moneyflow</a> - MoneyFlow<br>
    <a href="/api/v34/paper">/api/v34/paper</a> - Paper Distribution<br>
    <a href="/dashboard">/dashboard</a> - Alias<br>
    <a href="/health">/health</a> - Alias<br>
    <a href="/ping">/ping</a> - Alias
    </div>
    <div class="card" id="live">Loading...</div>
    <script>
    async function load(){{
        try{{
            let r=await fetch('/api/v34/health'); let j=await r.json();
            document.getElementById('live').innerHTML=`Status: ${{j.status}}<br>Queue: ${{j.queue}} | Open: ${{j.open}}<br>Uptime: ${{j.uptime_sec}}s<br>Exchanges: ${{j.exchanges.join(',')}}`;
        }}catch(e){{document.getElementById('live').innerHTML='Error loading health: '+e}}
    }}
    setInterval(load,5000); load();
    </script>
    </body></html>
    """)

@app.get("/api/v34/turtles")
def api_turtles():
    with LOCK: return JSONResponse(json_safe({"queue":STATE.get("vorauswahl_queue",[]),"count":len(STATE.get("vorauswahl_queue",[])),"universe_size":len(UNIVERSE_100)}))

@app.get("/api/v34/splinter")
def api_splinter():
    with LOCK: return JSONResponse(json_safe({"inspection":STATE.get("splinter_inspection",[])}))

@app.get("/api/v34/moneyflow")
def api_moneyflow():
    with LOCK:
        mf=STATE.get("money_flow",{})
        inflow=mf.get("avg_vol_change",0)
        inflow_val=200 if inflow<0 else 300+inflow*10
        outflow=abs(inflow)*1.5 if inflow<0 else inflow*0.6
        return JSONResponse(json_safe({"inflow":inflow_val,"outflow":outflow,"net_flow":inflow_val-outflow,"details":mf,"verdict":"GUT" if mf.get("avg_vol_change",0)<-10 else "VORSICHT" if mf.get("avg_vol_change",0)>20 else "NEUTRAL"}))

@app.get("/api/v34/active")
def api_active():
    with LOCK: return JSONResponse(json_safe({"active_calls":STATE.get("open_positions",[]),"closed_today":STATE.get("closed_today",[])[-5:],"equity":STATE.get("equity",0)}))

@app.post("/api/v34/wizdraw/close/{call_id}")
def api_wizdraw_close(call_id:str):
    with LOCK:
        res=wizdraw_manual_close(call_id,STATE)
        return JSONResponse(json_safe(res))

@app.get("/api/v34/paper")
def api_paper():
    with LOCK:
        trades=STATE.get("closed_today",[]); dist=paper_executor.evaluate_paper_distribution(trades)
        return JSONResponse(json_safe({"budget":paper_executor.budget,"current_equity":STATE.get("equity",0),"open_positions":STATE.get("open_positions",[]),"closed_today":trades[-20:],"distribution":dist,"config":paper_executor.config,"logs":paper_executor.logs[-50:]}))

@app.get("/api/v34/universe")
def api_universe(): return JSONResponse({"universe":UNIVERSE_100,"count":len(UNIVERSE_100)})

@app.get("/api/relax/status")
def api_status():
    with LOCK: update_status(); return JSONResponse(json_safe(STATE))

@app.get("/api/relax/scan")
def api_scan():
    with LOCK:
        try: res=scan_all(); return JSONResponse(json_safe(res))
        except Exception as e: return JSONResponse(json_safe({"status":"ERROR","reason":str(e)}))

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    try:
        with open(os.path.join(os.path.dirname(__file__),"templates/dashboard_v34.html"),"r",encoding="utf-8") as f: html=f.read(); return HTMLResponse(html)
    except: return HTMLResponse(f"<h1>TURTLE V3.4 RUNNING</h1><p>Equity {STATE.get('equity')} | <a href='/api/v34/health'>Health</a></p>")

@app.get("/relax", response_class=HTMLResponse)
def relax_dashboard():
    with LOCK: update_status(); s=json_safe(STATE.copy())
    status_color={"JAGT":"#00e676","PAUSE":"#ffea00","FEIERABEND_GEWINN":"#00b0ff","FEIERABEND_SCHUTZ":"#ff1744"}.get(s['status'],"#888")
    return HTMLResponse(f"<html><body style='background:#070707;color:#eee;font-family:monospace;padding:16px'><h1>RELAX {s['status']} <span style='background:{status_color};color:#000;padding:4px 12px;border-radius:20px'>{s['status']}</span> <a href='/dashboard' style='color:#39ff14'>-> V3.4</a></h1><div>{s['status_reason']} | Equity ${s['equity']:.2f}</div></body></html>")

scheduler=BackgroundScheduler()
scheduler.add_job(lambda: [update_status(), paper_monitor_positions(), monitor_positions(get_market_data, STATE)], 'interval', seconds=30, id='clan_monitor', max_instances=1, coalesce=True)
scheduler.add_job(run_turtle_scan, 'interval', seconds=120, id='turtle_2m', max_instances=1, coalesce=True, next_run_time=datetime.now(ZoneInfo("Europe/Berlin")))
scheduler.add_job(run_splinter_inspection, 'interval', minutes=5, id='splinter_5m', max_instances=1, coalesce=True)
scheduler.start()
update_status()
print("V3.4 FIXED gestartet - 2MIN SCAN - Health /api/v34/health + /health + /ping fuer UptimeRobot")
