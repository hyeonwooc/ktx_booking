"""
SRT / KTX auto-booking + auto-payment server
Run: python app.py  ->  http://localhost:5000
"""
import os, sys, uuid, threading, traceback, urllib.request, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

import json

# ── 개인 설정 로드 (config.json은 git에 올라가지 않음, config.example.json 참고) ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

def load_config():
    cfg = {"telegram_token": "", "telegram_chat_id": "", "user_id": "", "password": ""}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"[config] {CONFIG_PATH} 없음 → config.example.json을 복사해 config.json을 만드세요.")
    except Exception as e:
        print(f"[config] 읽기 실패: {e}")
    # 환경변수가 있으면 우선
    for k, env in (("telegram_token","TELEGRAM_TOKEN"),("telegram_chat_id","TELEGRAM_CHAT_ID"),
                   ("user_id","TRAIN_USER_ID"),("password","TRAIN_PASSWORD")):
        if os.environ.get(env): cfg[k] = os.environ[env]
    return cfg

CONFIG = load_config()
TELEGRAM_TOKEN   = CONFIG["telegram_token"]
TELEGRAM_CHAT_ID = CONFIG["telegram_chat_id"]

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 토큰/chat_id 미설정 → 알림 생략"); return
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        req  = urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")

app = Flask(__name__)

# 여러 예매 작업을 동시에 관리 (가는 편 / 오는 편 등)
jobs = {}
jobs_lock = threading.Lock()

def new_job():
    return {
        "state": {"running":False,"logs":[],"booked":False,"paid":False,"reservation_info":None,"label":""},
        "stop_event": threading.Event(),
        "lock": threading.Lock(),
    }

def fmt_phone(s):
    s = s.replace("-","").replace(" ","")
    if len(s)==11 and s.startswith("010"):
        return f"{s[:3]}-{s[3:7]}-{s[7:]}"
    return s

def run_srt(p, job):
    state = job["state"]; stop_event = job["stop_event"]; lock = job["lock"]
    def log(msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with lock:
            state["logs"].append({"ts":ts,"msg":msg,"level":level})
            if len(state["logs"])>300: state["logs"]=state["logs"][-300:]
        print(f"[{ts}] {msg}")
    try:
        from SRT import SRT
        from SRT.passenger import Adult
    except ImportError:
        log("SRT library missing. Run: pip install git+https://github.com/ryanking13/SRT.git","error"); return
    date=p["date"].replace("-","")
    dep_time=p["dep_time"].replace(":","")+"00"
    end_time=p.get("end_time","").replace(":","")+"00" if p.get("end_time") else ""
    seat=p.get("seat_type","일반실")
    interval=int(p.get("interval",30))
    qty=int(p.get("qty",1))
    allow_partial=bool(p.get("allow_partial"))
    selected=set(str(x) for x in (p.get("selected_trains") or []))
    card_num=p.get("card_number","").replace("-","").replace(" ","")
    expiry=p.get("card_expiry","").replace("/","").replace("-","")
    if len(expiry)==4: expiry=expiry[2:]+expiry[:2]  # MM/YY -> YYMM
    uid=fmt_phone(p['user_id'])
    log(f"[SRT] Logging in: {uid}")
    try: srt=SRT(uid,p["password"])
    except Exception as e: log(f"[SRT] Login failed: {e}","error"); return
    log("[SRT] Login OK!","success")
    if selected: log(f"[SRT] 선택된 {len(selected)}개 열차만 감시합니다.","info")
    while not stop_event.is_set():
        skip_interval = False
        try:
            log(f"[SRT] Searching {p['dep']} -> {p['arr']} {date} {dep_time[:4]}")
            trains=srt.search_train(p["dep"],p["arr"],date,dep_time,available_only=True)
            if not trains: log("[SRT] No trains found. Retrying...","warn")
            else:
                for t in trains:
                    if selected and str(t.train_number) not in selected: continue
                    if end_time and t.dep_time[:4]>end_time[:4]: continue
                    ok=((seat=="일반실" and t.general_seat_available()) or
                        (seat=="특실" and t.special_seat_available()) or
                        (seat=="상관없음" and (t.general_seat_available() or t.special_seat_available())))
                    if not ok: continue
                    log(f"[SRT] Seat found! {t} - Reserving...","success")
                    try:
                        rsv=None; got_qty=qty
                        try_list=list(range(qty,0,-1)) if allow_partial else [qty]
                        for q in try_list:
                            try:
                                rsv=srt.reserve(t,passengers=[Adult()]*q,special_seat=(seat=="특실"))
                                got_qty=q; break
                            except Exception as re:
                                if q>1 and allow_partial: log(f"[SRT] {q}매 실패, {q-1}매 시도...","warn")
                                else: raise re
                        if got_qty<qty: log(f"[SRT] {qty}매 중 {got_qty}매만 예매됨!","warn")
                        with lock: state["booked"]=True; state["reservation_info"]=str(rsv)
                        log(f"[SRT] Reserved! {rsv}","success")
                        if p.get("auto_pay") and card_num:
                            send_telegram(f"🚄 <b>SRT 예약 완료!</b>\n{rsv}\n\n자동결제 진행 중...")
                            log("[SRT] Paying...","info")
                            try:
                                srt.pay_with_card(rsv,number=card_num,password=p.get("card_pw",""),
                                    validation_number=p.get("card_birth","").replace("-",""),expire_date=expiry)
                                with lock: state["paid"]=True
                                log("[SRT] Payment complete!","success")
                                send_telegram("💳 <b>SRT 결제 완료!</b> 🎉")
                            except Exception as pe: log(f"[SRT] Payment failed: {pe}","error"); log("[SRT] Pay manually in SRT app!","warn")
                        else:
                            log("[SRT] 예약 완료! SRT 앱에서 20분 내 결제하세요.","warn")
                            send_telegram(f"🚄 <b>SRT 예약 완료!</b>\n{rsv}\n\n⚠️ 20분 내 SRT 앱에서 결제하세요!")
                        return
                    except Exception as e: log(f"[SRT] Reserve failed: {e}","warn")
                if not skip_interval: log("[SRT] No seats available.")
        except Exception as e:
            es=str(e).lower()
            if any(k in es for k in ("timeout","timed out","netfunnel","connection","10060")):
                log(f"[SRT] 네트워크 지연/타임아웃 - 5초 후 재시도합니다.","warn")
                if stop_event.wait(timeout=5): break
                continue
            log(f"[SRT] Error: {e}","error"); traceback.print_exc()
        if skip_interval: continue
        if stop_event.wait(timeout=interval): break
    log("[SRT] Stopped.")

def run_ktx(p, job):
    state = job["state"]; stop_event = job["stop_event"]; lock = job["lock"]
    def log(msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with lock:
            state["logs"].append({"ts":ts,"msg":msg,"level":level})
            if len(state["logs"])>300: state["logs"]=state["logs"][-300:]
        print(f"[{ts}] {msg}")
    try: import korail2
    except ImportError: log("korail2 missing. Run: pip install korail2","error"); return
    date=p["date"].replace("-","")
    dep_time=p["dep_time"].replace(":","")+"00"
    end_time=p.get("end_time","").replace(":","")+"00" if p.get("end_time") else ""
    interval=int(p.get("interval",30))
    qty=int(p.get("qty",1))
    allow_partial=bool(p.get("allow_partial"))
    selected=set(str(x) for x in (p.get("selected_trains") or []))
    card_num=p.get("card_number","").replace("-","").replace(" ","")
    expiry=p.get("card_expiry","").replace("/","").replace("-","")
    if len(expiry)==4: expiry=f"20{expiry[2:]}{expiry[:2]}"
    uid=fmt_phone(p['user_id'])
    log(f"[KTX] Logging in: {uid}")
    try: k=korail2.Korail(uid,p["password"])
    except Exception as e: log(f"[KTX] Login failed: {e}","error"); return
    log("[KTX] Login OK!","success")
    if selected: log(f"[KTX] 선택된 {len(selected)}개 열차만 감시합니다.","info")
    while not stop_event.is_set():
        skip_interval = False
        try:
            log(f"[KTX] Searching {p['dep']} -> {p['arr']} {date} {dep_time[:4]}")
            trains=k.search_train_available(p["dep"],p["arr"],date,dep_time)
            if not trains: log("[KTX] No trains. Retrying...","warn")
            else:
                for t in trains:
                    if selected and str(t.train_no) not in selected: continue
                    if end_time and t.dep_time[:4]>end_time[:4]: continue
                    try:
                        rsv=None; got_qty=qty
                        try_list=list(range(qty,0,-1)) if allow_partial else [qty]
                        for q in try_list:
                            try:
                                rsv=k.reserve(t,passengers=[korail2.AdultPassenger(q)])
                                got_qty=q; break
                            except korail2.SoldOutError:
                                if q>1 and allow_partial: log(f"[KTX] {q}매 실패, {q-1}매 시도...","warn")
                                else: raise
                        if got_qty<qty: log(f"[KTX] {qty}매 중 {got_qty}매만 예매됨!","warn")
                        with lock: state["booked"]=True; state["reservation_info"]=str(rsv)
                        log(f"[KTX] Reserved! {rsv}","success")
                        if p.get("auto_pay") and card_num:
                            log("[KTX] korail2 라이브러리는 자동결제를 지원하지 않습니다. 코레일 앱에서 10분 내 직접 결제하세요!","warn")
                            send_telegram(f"🚄 <b>KTX 예약 완료!</b>\n{rsv}\n\n⚠️ KTX는 자동결제가 지원되지 않습니다. 10분 내 코레일 앱에서 직접 결제하세요!")
                        else:
                            log("[KTX] 예약 완료! 코레일 앱에서 10분 내 결제하세요.","warn")
                            send_telegram(f"🚄 <b>KTX 예약 완료!</b>\n{rsv}\n\n⚠️ 10분 내 코레일 앱에서 결제하세요!")
                        return
                    except korail2.SoldOutError: log(f"[KTX] {t.dep_time[:4]} sold out. Next...","warn")
                    except Exception as e: log(f"[KTX] Reserve failed: {e}","warn")
                if not skip_interval: log("[KTX] No seats available.")
        except Exception as e:
            es=str(e).lower()
            if any(k in es for k in ("timeout","timed out","connection","10060")):
                log(f"[KTX] 네트워크 지연/타임아웃 - 5초 후 재시도합니다.","warn")
                if stop_event.wait(timeout=5): break
                continue
            log(f"[KTX] Error: {e}","error"); traceback.print_exc()
        if skip_interval: continue
        if stop_event.wait(timeout=interval): break
    log("[KTX] Stopped.")

@app.route("/")
def index(): return render_template_string(HTML, user_id=CONFIG["user_id"], password=CONFIG["password"])

@app.route("/api/start",methods=["POST"])
def api_start():
    p=request.json or {}
    for f in ["rail_type","user_id","password","dep","arr","date","dep_time"]:
        if not p.get(f): return jsonify({"ok":False,"msg":f"Missing: {f}"}),400
    job=new_job()
    job_id=uuid.uuid4().hex[:8]
    label=f"{p['rail_type']} {p['dep']}→{p['arr']}"
    job["state"]["label"]=label
    job["state"]["running"]=True
    with jobs_lock: jobs[job_id]=job
    target=run_srt if p["rail_type"]=="SRT" else run_ktx
    def worker():
        try: target(p,job)
        finally:
            with job["lock"]: job["state"]["running"]=False
    threading.Thread(target=worker,daemon=True).start()
    return jsonify({"ok":True,"job_id":job_id,"label":label})

@app.route("/api/stop",methods=["POST"])
def api_stop():
    p=request.json or {}
    with jobs_lock: job=jobs.get(p.get("job_id"))
    if not job: return jsonify({"ok":False,"msg":"No such job"}),404
    job["stop_event"].set()
    with job["lock"]: job["state"]["running"]=False
    return jsonify({"ok":True})

@app.route("/api/status")
def api_status():
    with jobs_lock: job=jobs.get(request.args.get("job_id"))
    if not job: return jsonify({"ok":False,"msg":"No such job"}),404
    with job["lock"]:
        s=job["state"]
        return jsonify({"ok":True,**{k:s[k] for k in ["running","booked","paid","reservation_info","label"]},"logs":s["logs"][-100:]})

@app.route("/api/timetable",methods=["POST"])
def api_timetable():
    p=request.json or {}
    for f in ["rail_type","user_id","password","dep","arr","date","dep_time"]:
        if not p.get(f): return jsonify({"ok":False,"msg":f"입력 필요: {f}"}),400
    date=p["date"].replace("-","")
    dep_time=p["dep_time"].replace(":","")+"00"
    uid=fmt_phone(p["user_id"])
    try:
        out=[]
        if p["rail_type"]=="SRT":
            from SRT import SRT
            srt=SRT(uid,p["password"])
            trains=srt.search_train(p["dep"],p["arr"],date,dep_time,available_only=False)
            for t in trains:
                out.append({"id":str(t.train_number),"name":getattr(t,"train_name","SRT"),
                    "no":t.train_number,"dep":t.dep_time[:4],"arr":t.arr_time[:4],
                    "general":bool(t.general_seat_available()),"special":bool(t.special_seat_available())})
        else:
            import korail2
            k=korail2.Korail(uid,p["password"])
            try: trains=k.search_train(p["dep"],p["arr"],date,dep_time,include_no_seats=True)
            except TypeError: trains=k.search_train(p["dep"],p["arr"],date,dep_time)
            for t in trains:
                gen=getattr(t,"has_general_seat",lambda:True)()
                spc=getattr(t,"has_special_seat",lambda:False)()
                out.append({"id":str(t.train_no),"name":getattr(t,"train_type_name","KTX"),
                    "no":t.train_no,"dep":t.dep_time[:4],"arr":t.arr_time[:4],
                    "general":bool(gen),"special":bool(spc)})
        return jsonify({"ok":True,"trains":out})
    except Exception as e:
        return jsonify({"ok":False,"msg":str(e)}),500

HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>기차표 자동예매</title>
<style>
:root{--bg:#0f1117;--sur:#1c1f2e;--sur2:#252839;--acc:#4f8ef7;--acc2:#7c6af7;
  --ok:#34d399;--warn:#fbbf24;--err:#f87171;--tx:#e2e8f0;--mu:#94a3b8;--bd:#2d3147;--r:12px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Segoe UI',sans-serif;min-height:100vh}
.wrap{max-width:980px;margin:0 auto;padding:28px 18px}
header{display:flex;align-items:center;gap:10px;margin-bottom:24px}
header h1{font-size:1.45rem;font-weight:700}
.badge{background:linear-gradient(135deg,var(--acc),var(--acc2));padding:3px 11px;border-radius:20px;font-size:.72rem;font-weight:600}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--sur);border:1px solid var(--bd);border-radius:var(--r);padding:20px}
.card h2{font-size:.78rem;font-weight:600;color:var(--mu);text-transform:uppercase;letter-spacing:.05em;margin-bottom:14px}
.field{margin-bottom:12px}
label{display:block;font-size:.78rem;color:var(--mu);margin-bottom:4px}
input,select{width:100%;background:var(--sur2);border:1px solid var(--bd);color:var(--tx);
  border-radius:8px;padding:8px 11px;font-size:.88rem;outline:none;transition:border .15s}
input:focus,select:focus{border-color:var(--acc)}

/* 토글 */
.tog-row{display:flex;gap:7px}
.tog{flex:1;padding:8px;border-radius:8px;border:1px solid var(--bd);background:var(--sur2);
  color:var(--mu);cursor:pointer;font-size:.88rem;transition:all .15s}
.tog.on{background:linear-gradient(135deg,var(--acc),var(--acc2));border-color:transparent;color:#fff;font-weight:700}

/* 검색 드롭다운 */
.sel-wrap{position:relative}
.sel-input{width:100%;background:var(--sur2);border:1px solid var(--bd);color:var(--tx);
  border-radius:8px;padding:8px 32px 8px 11px;font-size:.88rem;outline:none;cursor:pointer;transition:border .15s}
.sel-input:focus{border-color:var(--acc)}
.sel-arrow{position:absolute;right:10px;top:50%;transform:translateY(-50%);color:var(--mu);pointer-events:none;font-size:.8rem}
.sel-dropdown{position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--sur);
  border:1px solid var(--acc);border-radius:8px;z-index:999;max-height:220px;overflow-y:auto;display:none;box-shadow:0 8px 24px #0006}
.sel-dropdown.open{display:block}
.sel-search{width:100%;background:var(--sur2);border:none;border-bottom:1px solid var(--bd);
  color:var(--tx);padding:8px 12px;font-size:.85rem;outline:none}
.sel-list{padding:4px 0}
.sel-opt{padding:8px 14px;font-size:.86rem;cursor:pointer;transition:background .1s}
.sel-opt:hover,.sel-opt.focused{background:var(--sur2)}
.sel-opt.selected{color:var(--acc);font-weight:600}
.sel-group{padding:6px 14px 2px;font-size:.72rem;color:var(--mu);text-transform:uppercase;letter-spacing:.04em}
.sel-none{padding:12px 14px;color:var(--mu);font-size:.84rem;text-align:center}

/* 결제 */
.pay-toggle{display:flex;align-items:center;gap:8px;margin-bottom:10px;cursor:pointer}
.pay-toggle input[type=checkbox]{width:16px;height:16px;accent-color:var(--acc)}
.pay-toggle span{font-size:.84rem}
.pay-fields{display:none}
.pay-fields.show{display:block}
.warn-box{background:#2a1f00;border:1px solid var(--warn);border-radius:8px;
  padding:9px 12px;font-size:.76rem;color:var(--warn);line-height:1.6;margin-bottom:10px}
.card-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.save-note{font-size:.72rem;color:var(--ok);margin-top:4px}

/* 실행 */
.status-bar{display:flex;align-items:center;gap:9px;padding:10px 14px;border-radius:8px;
  background:var(--sur2);margin-bottom:10px;font-size:.84rem}
.dot{width:9px;height:9px;border-radius:50%}
.dot.idle{background:var(--mu)}.dot.running{background:var(--acc);animation:pulse 1s infinite}
.dot.booked{background:var(--ok)}.dot.paid{background:#a78bfa}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.btn{display:block;width:100%;padding:12px;border-radius:var(--r);border:none;
  cursor:pointer;font-size:.92rem;font-weight:700;transition:all .15s}
.btn-start{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff}
.btn-start:hover{opacity:.9;transform:translateY(-1px)}
.btn-stop{background:var(--sur2);border:1px solid var(--err);color:var(--err);margin-top:8px}
.btn-stop:hover{background:var(--err);color:#fff}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.res-box{background:#0f2a1a;border:1px solid var(--ok);border-radius:var(--r);
  padding:12px;margin-top:10px;font-size:.8rem;line-height:1.8;color:var(--ok);display:none}
.paid-box{background:#1a1030;border:1px solid #a78bfa;border-radius:var(--r);
  padding:12px;margin-top:8px;font-size:.9rem;color:#a78bfa;display:none;text-align:center;font-weight:700}
/* 시간표 */
.btn-tt{width:100%;padding:9px;border-radius:8px;border:1px solid var(--acc);background:var(--sur2);
  color:var(--acc);cursor:pointer;font-size:.85rem;font-weight:600;transition:all .15s}
.btn-tt:hover{background:var(--acc);color:#fff}
.btn-tt:disabled{opacity:.4;cursor:not-allowed}
.tt-box{margin-top:8px;border:1px solid var(--bd);border-radius:8px;background:var(--sur2);overflow:hidden}
.tt-head{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;
  border-bottom:1px solid var(--bd);font-size:.76rem;color:var(--mu)}
.tt-all{background:none;border:1px solid var(--bd);border-radius:6px;color:var(--acc);
  cursor:pointer;font-size:.72rem;padding:3px 8px}
#tt-list{max-height:220px;overflow-y:auto}
.tt-opt{display:flex;align-items:center;gap:9px;padding:8px 10px;font-size:.82rem;
  border-bottom:1px solid #1e2233;cursor:pointer}
.tt-opt:hover{background:#2b2f42}
.tt-opt:last-child{border-bottom:none}
.tt-opt input{width:15px;height:15px;accent-color:var(--acc);flex:none}
.tt-opt .tt-time{font-weight:600;color:var(--tx)}
.tt-opt .tt-meta{color:var(--mu);font-size:.74rem;margin-left:auto}
.tt-opt .seat-o{color:var(--ok)}.tt-opt .seat-x{color:var(--err)}
.tt-note{font-size:.72rem;color:var(--mu);margin-top:6px;line-height:1.5}
.pay-alert{background:#2a1a00;border:2px solid var(--warn);border-radius:var(--r);
  padding:14px;margin-top:10px;display:none;text-align:center}
.pay-alert .pay-alert-title{color:var(--warn);font-size:1rem;font-weight:700;margin-bottom:4px}
.pay-alert .pay-alert-count{color:#fde68a;font-size:1.4rem;font-weight:700;letter-spacing:.03em}
.pay-alert .pay-alert-sub{color:var(--mu);font-size:.74rem;margin-top:4px}
.tips{color:var(--mu);font-size:.74rem;margin-top:14px;line-height:1.9}
.tips li{margin-left:14px}

/* 로그 */
.log-box{background:#0a0c13;border:1px solid var(--bd);border-radius:var(--r);
  padding:12px 14px;height:300px;overflow-y:auto;font-family:'Consolas',monospace;font-size:.78rem;line-height:1.65}
.log-box .ts{color:#475569;margin-right:5px}
.log-box .info{color:var(--tx)}.log-box .success{color:var(--ok)}
.log-box .warn{color:var(--warn)}.log-box .error{color:var(--err)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span>🚄</span><h1>기차표 자동예매</h1><span class="badge">AUTO-PAY</span>
  </header>
  <div class="grid">

    <!-- 예매 조건 -->
    <div class="card">
      <h2>🎯 예매 조건</h2>
      <div class="field">
        <label>열차 종류</label>
        <div class="tog-row">
          <button class="tog on" id="btn-srt" onclick="setRail('SRT')">SRT</button>
          <button class="tog" id="btn-ktx" onclick="setRail('KTX')">KTX</button>
        </div>
      </div>
      <div class="field">
        <label>출발역</label>
        <div class="sel-wrap" id="dep-wrap">
          <input class="sel-input" id="dep-display" placeholder="역 선택..." readonly onclick="toggleSel('dep')">
          <span class="sel-arrow">▾</span>
          <div class="sel-dropdown" id="dep-dropdown">
            <input class="sel-search" id="dep-search" placeholder="역 검색..." oninput="filterSel('dep',this.value)" onkeydown="selKey(event,'dep')">
            <div class="sel-list" id="dep-list"></div>
          </div>
        </div>
        <input type="hidden" id="dep">
      </div>
      <div class="field">
        <label>도착역</label>
        <div class="sel-wrap" id="arr-wrap">
          <input class="sel-input" id="arr-display" placeholder="역 선택..." readonly onclick="toggleSel('arr')">
          <span class="sel-arrow">▾</span>
          <div class="sel-dropdown" id="arr-dropdown">
            <input class="sel-search" id="arr-search" placeholder="역 검색..." oninput="filterSel('arr',this.value)" onkeydown="selKey(event,'arr')">
            <div class="sel-list" id="arr-list"></div>
          </div>
        </div>
        <input type="hidden" id="arr">
      </div>
      <div class="field"><label>출발 날짜</label><input type="date" id="date" id="date"></div>
      <div class="field"><label>최소 출발 시각</label><input type="time" id="dep_time"></div>
      <div class="field"><label>최대 출발 시각 (선택)</label><input type="time" id="end_time"></div>
      <div class="field">
        <button class="btn-tt" id="btn-tt" onclick="loadTimetable()">🔍 시간표 조회</button>
        <div id="tt-box" class="tt-box" style="display:none">
          <div class="tt-head">
            <span id="tt-count">열차 선택</span>
            <button class="tt-all" onclick="toggleAllTT()">전체선택</button>
          </div>
          <div id="tt-list"></div>
        </div>
        <div class="tt-note" id="tt-note" style="display:none">체크한 열차만 예매합니다. 선택 안 하면 조건에 맞는 모든 열차를 예매합니다.</div>
      </div>
      <div class="field">
        <label>좌석 종류</label>
        <select id="seat_type">
          <option value="일반실">일반실</option>
          <option value="특실">특실</option>
          <option value="상관없음">상관없음</option>
        </select>
      </div>
      <div class="field">
        <label>매수</label>
        <div class="tog-row">
          <button class="tog on" id="qty-1" onclick="setQty(1)">1매</button>
          <button class="tog" id="qty-2" onclick="setQty(2)">2매</button>
          <button class="tog" id="qty-3" onclick="setQty(3)">3매</button>
          <button class="tog" id="qty-4" onclick="setQty(4)">4매</button>
        </div>
      </div>
      <div class="field" id="partial-field" style="display:none">
        <label class="pay-toggle" style="margin-bottom:0">
          <input type="checkbox" id="allow_partial">
          <span>좌석 부족 시 적은 매수라도 예매</span>
        </label>
      </div>
      <div class="field"><label>조회 간격 (초)</label><input type="number" id="interval" value="30" min="5" max="300"></div>
    </div>

    <!-- 계정 & 결제 -->
    <div class="card">
      <h2>🔐 계정 &amp; 결제</h2>
      <div class="field">
        <label id="label-id">SRT 회원번호 / 전화번호</label>
        <input type="text" id="user_id" placeholder="01000000000" autocomplete="username" value="{{ user_id }}">
      </div>
      <div class="field">
        <label>비밀번호</label>
        <input type="password" id="password" placeholder="••••••••" autocomplete="current-password" value="{{ password }}">
      </div>
      <hr style="border-color:var(--bd);margin:12px 0">
      <label class="pay-toggle">
        <input type="checkbox" id="auto_pay_chk" onchange="togglePay(this.checked)">
        <span>💳 예매 즉시 자동결제</span>
      </label>
      <div class="warn-box">⚠️ 카드 정보는 이 기기에만 저장됩니다.<br>서버나 외부로 전송되지 않습니다.</div>
      <div class="pay-fields" id="pay-fields">
        <div class="field">
          <label>카드 번호 (16자리)</label>
          <input type="text" id="card_number" placeholder="1234-5678-9012-3456" maxlength="19"
            oninput="fmtCard(this)" onchange="saveCard()">
        </div>
        <div class="card-row">
          <div class="field">
            <label>유효기간 (MM/YY)</label>
            <input type="text" id="card_expiry" placeholder="01/28" maxlength="5"
              oninput="fmtExpiry(this)" onchange="saveCard()">
          </div>
          <div class="field">
            <label>비밀번호 앞 2자리</label>
            <input type="password" id="card_pw" placeholder="••" maxlength="2" onchange="saveCard()">
          </div>
        </div>
        <div class="field">
          <label>생년월일 6자리 (YYMMDD)</label>
          <input type="text" id="card_birth" placeholder="901231" maxlength="6" onchange="saveCard()">
        </div>
        <div class="save-note">💾 카드 정보는 자동 저장됩니다</div>
      </div>
      <ul class="tips">
        <li>자동결제 미설정 시 앱에서 직접 결제</li>
        <li>SRT 미결제 20분 / KTX 10분 내 취소</li>
      </ul>
    </div>

    <!-- 실행 -->
    <div class="card" style="display:flex;flex-direction:column">
      <h2>🚀 실행</h2>
      <div class="status-bar">
        <div class="dot idle" id="status-dot"></div>
        <span id="status-text">대기 중</span>
      </div>
      <button class="btn btn-start" id="btn-start" onclick="startMacro()">🚀 자동예매 시작</button>
      <button class="btn btn-stop" id="btn-stop" onclick="stopMacro()" disabled>⏹ 중지</button>
      <div id="res-box" class="res-box"></div>
      <div id="pay-alert" class="pay-alert">
        <div class="pay-alert-title">⚠️ 지금 앱에서 결제하세요!</div>
        <div class="pay-alert-count" id="pay-countdown">--:--</div>
        <div class="pay-alert-sub">시간 내 미결제 시 예약 취소 후 자동 재검색</div>
      </div>
      <div id="paid-box" class="paid-box">💳 결제 완료! 🎉</div>
      <ul class="tips" style="margin-top:auto;padding-top:16px">
        <li>SRT: 수서 출발 노선</li>
        <li>KTX: 서울·용산 출발 노선</li>
        <li>빈 자리 발생 즉시 예매</li>
        <li>설정 시 예매 후 즉시 결제</li>
      </ul>
    </div>
  </div>

  <!-- 로그 -->
  <div class="card" style="margin-top:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <h2 style="margin:0">📋 실시간 로그</h2>
      <button onclick="clearLog()" style="background:none;border:none;color:var(--mu);cursor:pointer;font-size:.78rem">지우기</button>
    </div>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
// ── 역 데이터 ──
const STATIONS = {
  SRT: {
    "경부선": ["수서","동탄","평택지제","천안아산","오송","대전","김천구미","동대구","신경주","울산(통도사)","부산"],
    "호남선": ["수서","동탄","평택지제","천안아산","오송","공주","익산","정읍","광주송정","나주","목포"],
    "전라선": ["수서","동탄","평택지제","천안아산","오송","익산","전주","남원","순천","여수EXPO"],
    "경전선": ["수서","동탄","평택지제","천안아산","오송","대전","김천구미","동대구","밀양","창원중앙","창원","마산"],
    "동해선": ["수서","동탄","평택지제","천안아산","오송","대전","김천구미","동대구","신경주","울산(통도사)","포항"]
  },
  KTX: {
    "경부선": ["서울","용산","영등포","광명","수원","평택","천안아산","오송","대전","김천구미","동대구","경산","밀양","구포","부산"],
    "경전선": ["서울","용산","영등포","광명","수원","평택","천안아산","오송","대전","김천구미","동대구","밀양","창원중앙","창원","마산","진주"],
    "동해선": ["서울","용산","영등포","광명","수원","평택","천안아산","오송","대전","김천구미","동대구","신경주","울산","부산","태화강","포항"],
    "호남선": ["용산","광명","천안아산","오송","공주","익산","정읍","광주송정","나주","목포"],
    "전라선": ["용산","광명","천안아산","오송","익산","전주","남원","곡성","구례구","순천","여천","여수EXPO"],
    "강릉선": ["서울","청량리","상봉","양평","서원주","만종","횡성","둔내","평창","진부(오대산)","강릉"]
  }
};

// 중복 제거된 전체 역 목록
function getStations(rail) {
  const seen = new Set();
  const result = [];
  for (const [grp, stns] of Object.entries(STATIONS[rail])) {
    const unique = stns.filter(s => !seen.has(s));
    if (unique.length) { result.push({grp, stns: unique}); unique.forEach(s=>seen.add(s)); }
  }
  return result;
}

// ── 커스텀 드롭다운 ──
let selState = { dep: { val:"", focusIdx:-1 }, arr: { val:"", focusIdx:-1 } };
let currentRail = "SRT";
let currentQty = 1;


function renderList(id, query="") {
  const list = document.getElementById(id+"-list");
  const groups = getStations(currentRail);
  list.innerHTML = "";
  let count = 0;
  groups.forEach(({grp, stns}) => {
    const filtered = stns.filter(s => s.includes(query));
    if (!filtered.length) return;
    const g = document.createElement("div");
    g.className = "sel-group"; g.textContent = grp;
    list.appendChild(g);
    filtered.forEach(s => {
      const d = document.createElement("div");
      d.className = "sel-opt" + (selState[id].val===s?" selected":"");
      d.textContent = s;
      d.onclick = () => selectVal(id, s);
      list.appendChild(d); count++;
    });
  });
  if (!count) { list.innerHTML = '<div class="sel-none">검색 결과 없음</div>'; }
  selState[id].focusIdx = -1;
}

function toggleSel(id) {
  const dd = document.getElementById(id+"-dropdown");
  const isOpen = dd.classList.contains("open");
  closeAll();
  if (!isOpen) {
    dd.classList.add("open");
    document.getElementById(id+"-search").value = "";
    renderList(id, "");
    document.getElementById(id+"-search").focus();
  }
}

function closeAll() {
  document.querySelectorAll(".sel-dropdown").forEach(d=>d.classList.remove("open"));
}

function filterSel(id, q) { renderList(id, q); }

function selectVal(id, val) {
  selState[id].val = val;
  document.getElementById(id+"-display").value = val;
  document.getElementById(id).value = val;
  closeAll();
  if(typeof clearTimetable==="function") clearTimetable();
}

function selKey(e, id) {
  const opts = [...document.querySelectorAll(`#${id}-list .sel-opt`)];
  if (e.key==="Escape") { closeAll(); return; }
  if (e.key==="Enter") {
    e.preventDefault();
    const idx = selState[id].focusIdx >= 0 ? selState[id].focusIdx : 0;
    if (opts[idx]) opts[idx].click();
    return;
  }
  if (!opts.length) return;
  if (e.key==="ArrowDown") { selState[id].focusIdx=Math.min(selState[id].focusIdx+1,opts.length-1); }
  else if (e.key==="ArrowUp") { selState[id].focusIdx=Math.max(selState[id].focusIdx-1,0); }
  opts.forEach((o,i)=>o.classList.toggle("focused",i===selState[id].focusIdx));
  if (selState[id].focusIdx>=0) opts[selState[id].focusIdx].scrollIntoView({block:"nearest"});
}

document.addEventListener("click", e => {
  if (!e.target.closest(".sel-wrap")) closeAll();
});

function setRail(r) {
  currentRail = r;
  document.getElementById("btn-srt").classList.toggle("on",r==="SRT");
  document.getElementById("btn-ktx").classList.toggle("on",r==="KTX");
  document.getElementById("label-id").textContent = r==="SRT"?"SRT 회원번호 / 전화번호":"코레일 회원번호 / 전화번호";
  // 역 초기화
  ["dep","arr"].forEach(id=>{
    selState[id].val="";
    document.getElementById(id+"-display").value="";
    document.getElementById(id).value="";
  });
  if(typeof clearTimetable==="function") clearTimetable();
}

// ── 카드 정보 저장/불러오기 ──
function saveCard() {
  const data = {
    card_number: document.getElementById("card_number").value,
    card_expiry: document.getElementById("card_expiry").value,
    card_birth:  document.getElementById("card_birth").value,
    card_pw:     document.getElementById("card_pw").value,
    auto_pay:    document.getElementById("auto_pay_chk").checked,
  };
  localStorage.setItem("ktx_card", JSON.stringify(data));
}

function loadCard() {
  try {
    const d = JSON.parse(localStorage.getItem("ktx_card")||"{}");
    if (d.card_number) document.getElementById("card_number").value=d.card_number;
    if (d.card_expiry) document.getElementById("card_expiry").value=d.card_expiry;
    if (d.card_birth)  document.getElementById("card_birth").value=d.card_birth;
    if (d.card_pw)     document.getElementById("card_pw").value=d.card_pw;
    if (d.auto_pay)    { document.getElementById("auto_pay_chk").checked=true; togglePay(true); }
  } catch(e) {}
}

function setQty(n) {
  currentQty = n;
  [1,2,3,4].forEach(i => document.getElementById("qty-"+i).classList.toggle("on", i===n));
  document.getElementById("partial-field").style.display = n>1 ? "block" : "none";
  if (n<=1) document.getElementById("allow_partial").checked = false;
}

function togglePay(on) {
  document.getElementById("pay-fields").classList.toggle("show",on);
  saveCard();
}
function fmtCard(el) {
  let v=el.value.replace(/\D/g,"").slice(0,16);
  el.value=v.replace(/(.{4})/g,"$1-").replace(/-$/,"");
}
function fmtExpiry(el) {
  let v=el.value.replace(/\D/g,"").slice(0,4);
  if(v.length>2) v=v.slice(0,2)+"/"+v.slice(2);
  el.value=v;
}

// ── 시간표 조회 ──
let ttTrains=[];

async function loadTimetable() {
  const uid=document.getElementById("user_id").value.trim();
  const pw=document.getElementById("password").value;
  const dep=document.getElementById("dep").value, arr=document.getElementById("arr").value;
  const date=document.getElementById("date").value, dtime=document.getElementById("dep_time").value;
  if(!uid||!pw){alert("아이디와 비밀번호를 먼저 입력하세요.");return;}
  if(!dep||!arr){alert("출발역과 도착역을 선택하세요.");return;}
  if(!date){alert("날짜를 선택하세요.");return;}
  const btn=document.getElementById("btn-tt");
  btn.disabled=true; btn.textContent="⏳ 조회 중...";
  try{
    const r=await fetch("/api/timetable",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({rail_type:currentRail,user_id:uid,password:pw,dep,arr,date,dep_time:dtime})});
    const d=await r.json();
    if(!d.ok){alert("시간표 조회 실패: "+d.msg);return;}
    ttTrains=d.trains||[];
    renderTimetable();
  }catch(e){alert("조회 오류: "+e);}
  finally{btn.disabled=false; btn.textContent="🔍 시간표 조회";}
}

function renderTimetable() {
  const box=document.getElementById("tt-box"), list=document.getElementById("tt-list");
  const note=document.getElementById("tt-note");
  if(!ttTrains.length){
    box.style.display="block";note.style.display="none";
    list.innerHTML='<div style="padding:14px;text-align:center;color:var(--mu);font-size:.82rem">열차가 없습니다.</div>';
    document.getElementById("tt-count").textContent="열차 0개";
    return;
  }
  list.innerHTML="";
  ttTrains.forEach((t,i)=>{
    const row=document.createElement("label");
    row.className="tt-opt";
    const g=t.general?'<span class="seat-o">일반 O</span>':'<span class="seat-x">일반 X</span>';
    const s=t.special?'<span class="seat-o">특실 O</span>':'<span class="seat-x">특실 X</span>';
    row.innerHTML='<input type="checkbox" class="tt-chk" data-id="'+t.id+'">'
      +'<span class="tt-time">'+t.dep+' → '+t.arr+'</span>'
      +'<span class="tt-meta">'+esc(t.name)+' #'+t.no+' · '+g+' / '+s+'</span>';
    list.appendChild(row);
  });
  box.style.display="block"; note.style.display="block";
  updateTTCount();
  list.querySelectorAll(".tt-chk").forEach(c=>c.addEventListener("change",updateTTCount));
}

function updateTTCount(){
  const n=document.querySelectorAll("#tt-list .tt-chk:checked").length;
  document.getElementById("tt-count").textContent = n>0 ? (n+"개 선택됨") : "열차 선택 (전체 "+ttTrains.length+")";
}

function toggleAllTT(){
  const chks=[...document.querySelectorAll("#tt-list .tt-chk")];
  const allOn=chks.every(c=>c.checked);
  chks.forEach(c=>c.checked=!allOn);
  updateTTCount();
}

function getSelectedTrains(){
  return [...document.querySelectorAll("#tt-list .tt-chk:checked")].map(c=>c.dataset.id);
}

function clearTimetable(){
  ttTrains=[];
  document.getElementById("tt-box").style.display="none";
  document.getElementById("tt-note").style.display="none";
  document.getElementById("tt-list").innerHTML="";
}

// ── 예매 시작/중지 ──
let polling=null, lastIdx=0, autoScroll=true, currentJobId=null;

async function startMacro() {
  const params = {
    rail_type:   currentRail,
    user_id:     document.getElementById("user_id").value.trim(),
    password:    document.getElementById("password").value,
    dep:         document.getElementById("dep").value,
    arr:         document.getElementById("arr").value,
    date:        document.getElementById("date").value,
    dep_time:    document.getElementById("dep_time").value,
    end_time:    document.getElementById("end_time").value,
    seat_type:   document.getElementById("seat_type").value,
    interval:    document.getElementById("interval").value,
    qty:         currentQty,
    allow_partial: document.getElementById("allow_partial").checked,
    selected_trains: getSelectedTrains(),
    auto_pay:    document.getElementById("auto_pay_chk").checked,
    card_number: document.getElementById("card_number").value,
    card_expiry: document.getElementById("card_expiry").value,
    card_birth:  document.getElementById("card_birth").value,
    card_pw:     document.getElementById("card_pw").value,
  };
  if (!params.user_id||!params.password){alert("아이디와 비밀번호를 입력하세요.");return;}
  if (!params.dep||!params.arr){alert("출발역과 도착역을 선택하세요.");return;}
  if (!params.date){alert("날짜를 선택하세요.");return;}
  if (params.auto_pay&&!params.card_number){alert("카드 번호를 입력하세요.");return;}
  lastIdx=0;
  document.getElementById("log-box").innerHTML="";
  document.getElementById("res-box").style.display="none";
  document.getElementById("paid-box").style.display="none";
  document.getElementById("pay-alert").style.display="none";
  const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(params)});
  const d=await r.json();
  if(!d.ok){alert(d.msg);return;}
  currentJobId=d.job_id;
  sessionStorage.setItem("job_id",currentJobId);
  if(d.label) document.title=d.label+" - 기차표 자동예매";
  setUI(true);
  if(polling)clearInterval(polling);
  polling=setInterval(poll,1500);
}

async function stopMacro() {
  if(currentJobId) await fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({job_id:currentJobId})});
  sessionStorage.removeItem("job_id");
  setUI(false); clearInterval(polling); polling=null;
}

function fmtCountdown(deadlineIso) {
  const diff = Math.max(0, new Date(deadlineIso) - Date.now());
  const m = Math.floor(diff/60000);
  const s = Math.floor((diff%60000)/1000);
  return String(m).padStart(2,"0")+":"+String(s).padStart(2,"0");
}

async function poll() {
  if(!currentJobId) return;
  try {
    const d=await (await fetch("/api/status?job_id="+currentJobId)).json();
    if(!d.ok){ // 작업 없음 (서버 재시작 등)
      sessionStorage.removeItem("job_id");currentJobId=null;
      setUI(false);clearInterval(polling);polling=null;return;
    }
    const box=document.getElementById("log-box");
    d.logs.slice(lastIdx).forEach(e=>{
      const ln=document.createElement("div");
      ln.innerHTML='<span class="ts">['+e.ts+']</span><span class="'+e.level+'">'+esc(e.msg)+'</span>';
      box.appendChild(ln);
    });
    lastIdx=d.logs.length;
    if(autoScroll)box.scrollTop=box.scrollHeight;
    const dot=document.getElementById("status-dot"),txt=document.getElementById("status-text");
    const payAlert=document.getElementById("pay-alert");
    const resBox=document.getElementById("res-box");
    if(d.paid){
      dot.className="dot paid";txt.textContent="💳 결제 완료!";
      document.getElementById("paid-box").style.display="block";
      resBox.textContent=d.reservation_info||"";resBox.style.display="block";
      payAlert.style.display="none";
      sessionStorage.removeItem("job_id");
      setUI(false);clearInterval(polling);polling=null;
    } else if(d.booked && !d.running){
      dot.className="dot booked";txt.textContent="✅ 예매 완료!";
      resBox.textContent=d.reservation_info||"";resBox.style.display="block";
      payAlert.style.display="none";
      sessionStorage.removeItem("job_id");
      setUI(false);clearInterval(polling);polling=null;
    } else if(d.running){
      dot.className="dot running";txt.textContent="🔄 조회 중…";
      payAlert.style.display="none";
      resBox.style.display="none";
    } else {
      dot.className="dot idle";txt.textContent="대기 중";
      payAlert.style.display="none";
      sessionStorage.removeItem("job_id");
      setUI(false);clearInterval(polling);polling=null;
    }
  } catch(e){console.error(e);}
}

function setUI(on){
  document.getElementById("btn-start").disabled=on;
  document.getElementById("btn-stop").disabled=!on;
}
function clearLog(){document.getElementById("log-box").innerHTML="";lastIdx=0;}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
document.getElementById("log-box").addEventListener("scroll",function(){
  autoScroll=this.scrollHeight-this.scrollTop-this.clientHeight<30;
});

// ── 초기화 ──
(function() {
  const pad = n => String(n).padStart(2,"0");

  // 날짜 input: 오늘 이후만 선택 가능
  const todayStr = new Date().toISOString().split("T")[0];
  const dateEl = document.getElementById("date");
  dateEl.min = todayStr;
  dateEl.value = todayStr;

  // 시각 input: 현재 시각 기준 다음 정각 (예: 14:23 → 15:00)
  const now = new Date();
  const nextHour = new Date(now);
  nextHour.setHours(now.getHours() + 1, 0, 0, 0);
  document.getElementById("dep_time").value = `${pad(nextHour.getHours())}:00`;

  // 날짜 바뀌면 시간 재검증
  dateEl.addEventListener("change", function() {
    const sel = new Date(this.value);
    const tod = new Date(todayStr);
    if (sel.toDateString() === tod.toDateString()) {
      // 오늘이면 현재 이후 시각으로
      const h = new Date();
      h.setHours(h.getHours()+1, 0, 0, 0);
      document.getElementById("dep_time").value = `${pad(h.getHours())}:00`;
    } else {
      // 미래 날짜면 첫 열차 시각
      document.getElementById("dep_time").value = "06:00";
    }
  });
})();
renderList("dep"); renderList("arr");
loadCard();

// 새로고침 시 진행 중이던 작업 복원 (탭별)
(function(){
  const saved = sessionStorage.getItem("job_id");
  if(saved){
    currentJobId = saved;
    setUI(true);
    if(polling)clearInterval(polling);
    polling=setInterval(poll,1500);
    poll();
  }
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Server started: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
