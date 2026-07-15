from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from SmartApi import SmartConnect
import pyotp
import requests
from datetime import datetime, timedelta
import uvicorn
import threading
import time
import json
import os
import re

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY   = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD  = os.getenv("ANGEL_PASSWORD")
TOTP_KEY  = os.getenv("ANGEL_TOTP_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "8935646874:AAGhni4W84AwNmcm5YM9WMTSs16NpfHcdxU")
SUBSCRIBERS_FILE = "subscribers.json"

SYMBOL_CONFIG = {
    "NIFTY":  {"token": "99926000", "exchange": "NSE"},
    "SENSEX": {"token": "99919000", "exchange": "BSE"},
}
FUTURES_SEARCH_CONFIG = {
    "NIFTY":  {"exchange": "NFO", "search": "NIFTY"},
    "SENSEX": {"exchange": "BFO", "search": "SENSEX"},
}

last_signal_sent    = {}
futures_token_cache = {}
last_cpr_date       = None
ADX_THRESHOLD       = 20

IST_OFFSET = timedelta(hours=5, minutes=30)

def now_ist():
    """
    Render's server clock runs in UTC, not IST. Every place that needs
    'the current time' for display or scheduling (Telegram message
    timestamps, the 9 AM CPR window, candle date-range fetching) must
    use this instead of datetime.now(), or everything is off by 5:30.
    """
    return datetime.utcnow() + IST_OFFSET

def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subs, f, indent=2)

subscribers = load_subscribers()

def get_last_trading_day():
    today = now_ist()
    wd = today.weekday()
    if wd == 5:   today -= timedelta(days=1)
    elif wd == 6: today -= timedelta(days=2)
    return today

def get_angel_session():
    try:
        obj  = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_KEY).now()
        data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        if data["status"] == False:
            raise Exception("Login failed: " + str(data))
        return obj
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

def send_to_chat(chat_id, message: str):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_to_all(message: str):
    subs = load_subscribers()
    for name, chat_id in subs.items():
        send_to_chat(chat_id, message)

def poll_telegram():
    last_update_id = 0
    while True:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
            data = resp.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                name    = msg.get("chat", {}).get("first_name", "User")
                if text.startswith("/start"):
                    subs = load_subscribers()
                    subs[name] = chat_id
                    save_subscribers(subs)
                    subscribers[name] = chat_id
                    send_to_chat(chat_id, f"✅ <b>Welcome {name}!</b>\n\nSubscribed to NiftySignal PRO!\n\n📊 Daily CPR at 9 AM IST\n📈 EMA 9×26 + ADX + DI + VWAP signals (3-level targets)\n\n<i>NiftySignal PRO</i>")
                elif text.startswith("/stop"):
                    subs = load_subscribers()
                    if name in subs:
                        del subs[name]
                        save_subscribers(subs)
                        subscribers.pop(name, None)
                    send_to_chat(chat_id, "❌ Unsubscribed.")
                elif text.startswith("/status"):
                    send_to_chat(chat_id, f"📊 <b>NiftySignal PRO</b>\n\n✅ Running\n👤 Subscribers: {len(load_subscribers())}\n🕐 {now_ist().strftime('%d %b %Y %I:%M %p')} IST")
        except Exception as e:
            print(f"Telegram poll error: {e}")
        time.sleep(2)

def calculate_ema(prices, period):
    if len(prices) < period: return []
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    result = [None] * (period - 1)
    result.append(ema)
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
        result.append(ema)
    return result

def calculate_atr(candles, period=14):
    n = len(candles)
    atr = [None] * n
    if n < period + 1: return atr
    tr = [None] * n
    for i in range(1, n):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i-1][4]
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    atr[period] = sum(tr[1:period+1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

def calculate_adx_di(candles, period=14):
    n = len(candles)
    adx = [None]*n; plus_di = [None]*n; minus_di = [None]*n
    if n < period * 2: return adx, plus_di, minus_di
    pdm=[0]*n; mdm=[0]*n; tr=[0]*n
    for i in range(1, n):
        high, low = candles[i][2], candles[i][3]
        ph, pl, pc = candles[i-1][2], candles[i-1][3], candles[i-1][4]
        up, down = high - ph, pl - low
        pdm[i] = up if (up > down and up > 0) else 0
        mdm[i] = down if (down > up and down > 0) else 0
        tr[i] = max(high - low, abs(high - pc), abs(low - pc))
    smTR=[None]*n; smP=[None]*n; smM=[None]*n
    smTR[period]=sum(tr[1:period+1]); smP[period]=sum(pdm[1:period+1]); smM[period]=sum(mdm[1:period+1])
    for i in range(period+1, n):
        smTR[i]=smTR[i-1]-smTR[i-1]/period+tr[i]
        smP[i]=smP[i-1]-smP[i-1]/period+pdm[i]
        smM[i]=smM[i-1]-smM[i-1]/period+mdm[i]
    dx=[None]*n
    for i in range(period, n):
        if not smTR[i]: continue
        pdi=100*smP[i]/smTR[i]; mdi=100*smM[i]/smTR[i]
        plus_di[i]=pdi; minus_di[i]=mdi
        if (pdi+mdi)==0: continue
        dx[i]=100*abs(pdi-mdi)/(pdi+mdi)
    valid=[d for d in dx[period:period*2] if d is not None]
    if len(valid)<period: return adx, plus_di, minus_di
    adx[period*2-1]=sum(valid)/period
    for i in range(period*2, n):
        if dx[i] is None: continue
        adx[i]=(adx[i-1]*(period-1)+dx[i])/period
    return adx, plus_di, minus_di

def get_signal_strength(adx_val, plus_di, minus_di):
    """
    Rough confidence tiering — NOT a probability guarantee, just a
    heuristic combining trend strength (ADX) with how decisively one
    side is leading (DI spread), so weak-but-still-qualifying signals
    can be told apart from strong ones.
    """
    di_spread = abs(plus_di - minus_di)
    if adx_val >= 35 and di_spread >= 15:
        return "STRONG 🔥"
    elif adx_val >= 25 and di_spread >= 8:
        return "MODERATE ⚡"
    else:
        return "WEAK ⚠️"

def get_futures_contract(obj, symbol):
    today_str = now_ist().strftime("%Y-%m-%d")
    cached = futures_token_cache.get(symbol)
    if cached and cached["date"] == today_str: return cached["token"], cached["exchange"]
    cfg = FUTURES_SEARCH_CONFIG.get(symbol)
    if not cfg: return None, None
    try:
        result = obj.searchScrip(cfg["exchange"], cfg["search"])
        scrips = result.get("data", []) if result else []
        candidates = []
        for s in scrips:
            tsym = s.get("tradingsymbol", "")
            if not tsym.endswith("FUT"): continue
            if symbol == "NIFTY" and "BANK" in tsym.upper(): continue
            match = re.search(r'(\d{2}[A-Z]{3}\d{2})', tsym)
            if not match: continue
            try: expiry = datetime.strptime(match.group(1), "%d%b%y")
            except: continue
            candidates.append((expiry, s.get("symboltoken"), s.get("exchange", cfg["exchange"]), tsym))
        if not candidates: return None, None
        candidates.sort(key=lambda x: x[0])
        chosen = candidates[0]
        futures_token_cache[symbol] = {"token": chosen[1], "exchange": chosen[2], "date": today_str, "tradingsymbol": chosen[3]}
        return chosen[1], chosen[2]
    except Exception as e:
        print(f"Futures token error {symbol}: {e}")
        return None, None

def fetch_futures_candles(obj, symbol, interval, from_str, to_str):
    token, exchange = get_futures_contract(obj, symbol)
    if not token: return []
    try:
        resp = obj.getCandleData({"exchange": exchange, "symboltoken": token, "interval": interval, "fromdate": from_str, "todate": to_str})
        return resp.get("data", []) if resp else []
    except Exception as e:
        print(f"Futures candle error {symbol}: {e}")
        return []

def calculate_vwap_from_futures(spot_candles, futures_candles):
    n = len(spot_candles)
    vwap = [None] * n
    vol_map = {c[0]: c[5] for c in futures_candles if len(c) > 5}
    cum_pv = cum_vol = cum_price = cum_count = 0; current_day = None
    for i in range(n):
        day = str(spot_candles[i][0])[:10]
        if day != current_day:
            current_day = day; cum_pv = cum_vol = cum_price = cum_count = 0
        tp = (spot_candles[i][2] + spot_candles[i][3] + spot_candles[i][4]) / 3
        vol = vol_map.get(spot_candles[i][0], 0)
        cum_price += tp; cum_count += 1
        if vol and vol > 0:
            cum_pv += tp * vol; cum_vol += vol; vwap[i] = cum_pv / cum_vol
        else:
            vwap[i] = (cum_pv / cum_vol) if cum_vol > 0 else (cum_price / cum_count)
    return vwap

def calculate_cpr(prev_high, prev_low, prev_close):
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2; tc = (pivot - bc) + pivot
    return {"pivot": round(pivot,2), "bc": round(min(bc,tc),2), "tc": round(max(bc,tc),2), "prev_high": round(prev_high,2), "prev_low": round(prev_low,2), "prev_close": round(prev_close,2)}

def get_previous_day_ohlc(obj, symbol):
    config = SYMBOL_CONFIG[symbol]
    to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
    from_date = to_date - timedelta(days=10)
    resp = obj.getCandleData({"exchange": config["exchange"], "symboltoken": config["token"], "interval": "ONE_DAY", "fromdate": from_date.strftime("%Y-%m-%d %H:%M"), "todate": to_date.strftime("%Y-%m-%d %H:%M")})
    candles = resp.get("data", []) if resp else []
    if not candles: return None
    last = candles[-1]; return last[2], last[3], last[4]

def cpr_scheduler():
    global last_cpr_date
    while True:
        try:
            now = now_ist()
            today_str = now.strftime("%Y-%m-%d")
            in_window = now.weekday() < 5 and ((now.hour == 8 and now.minute >= 55) or (now.hour == 9 and now.minute <= 5))
            if in_window and last_cpr_date != today_str:
                obj = get_angel_session()
                lines = ["📍 <b>Daily CPR Levels</b>\n<i>(Reference only — NOT a buy/sell signal)</i>\n"]
                for symbol in SYMBOL_CONFIG:
                    ohlc = get_previous_day_ohlc(obj, symbol)
                    if not ohlc: continue
                    high, low, close = ohlc; cpr = calculate_cpr(high, low, close)
                    lines.append(f"\n📊 <b>{symbol}</b>\nPivot: ₹{cpr['pivot']:,}\nTC: ₹{cpr['tc']:,}  |  BC: ₹{cpr['bc']:,}\n<i>(Prev Day H: {cpr['prev_high']:,} L: {cpr['prev_low']:,} C: {cpr['prev_close']:,})</i>")
                lines.append("\n\n<i>Reference only for today's rough bias. Actual BUY/SELL alerts come separately when EMA + ADX + DI + Futures-VWAP all confirm.</i>")
                send_to_all("\n".join(lines))
                last_cpr_date = today_str
                print(f"CPR sent for {today_str} at {now.strftime('%I:%M %p')} IST")
        except Exception as e:
            print(f"CPR error: {e}")
        time.sleep(30)

def detect_crossover(spot_candles, futures_candles, symbol, timeframe):
    if len(spot_candles) < 60: return
    closes = [c[4] for c in spot_candles]
    ema9 = calculate_ema(closes, 9); ema26 = calculate_ema(closes, 26)
    atr = calculate_atr(spot_candles, 14)
    adx, plus_di, minus_di = calculate_adx_di(spot_candles, 14)
    vwap = calculate_vwap_from_futures(spot_candles, futures_candles)
    i = len(closes) - 1
    if not ema9[i] or not ema9[i-1] or not ema26[i] or not ema26[i-1]: return
    if atr[i] is None or adx[i] is None or vwap[i] is None: return
    if plus_di[i] is None or minus_di[i] is None: return
    signal_key = f"{symbol}_{timeframe}"; last_time = spot_candles[-1][0]; price = closes[-1]
    is_trending = adx[i] > ADX_THRESHOLD
    bulls_lead = plus_di[i] > minus_di[i]
    bears_lead = minus_di[i] > plus_di[i]
    strength = get_signal_strength(adx[i], plus_di[i], minus_di[i])
    ts = now_ist().strftime('%d %b %Y %I:%M %p') + " IST"

    if ema9[i-1] <= ema26[i-1] and ema9[i] > ema26[i]:
        if is_trending and bulls_lead and price > vwap[i]:
            key = f"{signal_key}_BUY_{last_time}"
            if last_signal_sent.get(signal_key) != key:
                last_signal_sent[signal_key] = key
                sl = round(price - 1.5 * atr[i], 2)
                t1 = round(price + 1.0 * atr[i], 2)
                t2 = round(price + 2.0 * atr[i], 2)
                t3 = round(price + 3.0 * atr[i], 2)
                send_to_all(
                    f"🟢 <b>BUY SIGNAL</b> — {strength}\n\n"
                    f"📊 <b>{symbol}</b> | {timeframe}\n"
                    f"💰 Price: <b>₹{price:,.2f}</b>\n"
                    f"🛑 SL: ₹{sl:,.2f}\n"
                    f"🎯 T1: ₹{t1:,.2f}  T2: ₹{t2:,.2f}  T3: ₹{t3:,.2f}\n"
                    f"📈 ADX: {adx[i]:.1f} | +DI: {plus_di[i]:.1f} > -DI: {minus_di[i]:.1f} ✅\n"
                    f"💹 Above Futures-VWAP ₹{vwap[i]:,.2f}\n"
                    f"🕐 {ts}\n\n"
                    f"<i>NiftySignal PRO</i>"
                )
    elif ema9[i-1] >= ema26[i-1] and ema9[i] < ema26[i]:
        if is_trending and bears_lead and price < vwap[i]:
            key = f"{signal_key}_SELL_{last_time}"
            if last_signal_sent.get(signal_key) != key:
                last_signal_sent[signal_key] = key
                sl = round(price + 1.5 * atr[i], 2)
                t1 = round(price - 1.0 * atr[i], 2)
                t2 = round(price - 2.0 * atr[i], 2)
                t3 = round(price - 3.0 * atr[i], 2)
                send_to_all(
                    f"🔴 <b>SELL SIGNAL</b> — {strength}\n\n"
                    f"📊 <b>{symbol}</b> | {timeframe}\n"
                    f"💰 Price: <b>₹{price:,.2f}</b>\n"
                    f"🛑 SL: ₹{sl:,.2f}\n"
                    f"🎯 T1: ₹{t1:,.2f}  T2: ₹{t2:,.2f}  T3: ₹{t3:,.2f}\n"
                    f"📉 ADX: {adx[i]:.1f} | -DI: {minus_di[i]:.1f} > +DI: {plus_di[i]:.1f} ✅\n"
                    f"💹 Below Futures-VWAP ₹{vwap[i]:,.2f}\n"
                    f"🕐 {ts}\n\n"
                    f"<i>NiftySignal PRO</i>"
                )

def signal_scanner():
    while True:
        try:
            obj = get_angel_session()
            to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
            now = now_ist()
            if now.weekday() < 5 and now.hour >= 9: to_date = now
            from_date = to_date - timedelta(days=5)
            from_str = from_date.strftime("%Y-%m-%d %H:%M"); to_str = to_date.strftime("%Y-%m-%d %H:%M")
            for symbol, config in SYMBOL_CONFIG.items():
                try:
                    resp = obj.getCandleData({"exchange": config["exchange"], "symboltoken": config["token"], "interval": "FIVE_MINUTE", "fromdate": from_str, "todate": to_str})
                    candles = resp.get("data", [])
                    futures = fetch_futures_candles(obj, symbol, "FIVE_MINUTE", from_str, to_str)
                    if candles: detect_crossover(candles, futures, symbol, "5min"); print(f"Scanned {symbol} — {len(candles)} candles at {now.strftime('%I:%M %p')} IST")
                except Exception as e:
                    print(f"Scanner error {symbol}: {e}")
                time.sleep(5)
        except Exception as e:
            print(f"Session error: {e}")
        time.sleep(300)

@app.get("/")
def root():
    subs = load_subscribers()
    return {"status": "NiftySignal PRO running", "subscribers": len(subs), "names": list(subs.keys()), "server_time_ist": now_ist().strftime('%d %b %Y %I:%M:%S %p')}

@app.head("/")
def root_head(): return {}

@app.get("/subscribers")
def get_subscribers(): return load_subscribers()

@app.get("/test-telegram")
def test_telegram():
    send_to_all("✅ <b>NiftySignal PRO</b>\n\nTelegram alerts working!")
    return {"status": "Sent!"}

@app.get("/test-signal")
def test_signal():
    price = 24250.00
    atr_est = 45.0
    strength = get_signal_strength(27.3, 28.5, 18.2)
    sl = round(price - 1.5*atr_est, 2); t1 = round(price + atr_est, 2); t2 = round(price + 2*atr_est, 2); t3 = round(price + 3*atr_est, 2)
    send_to_all(
        f"🟢 <b>BUY SIGNAL</b> (TEST) — {strength}\n\n"
        f"📊 <b>NIFTY</b> | 5min\n"
        f"💰 Price: <b>₹{price:,.2f}</b>\n"
        f"🛑 SL: ₹{sl:,.2f}\n"
        f"🎯 T1: ₹{t1:,.2f}  T2: ₹{t2:,.2f}  T3: ₹{t3:,.2f}\n"
        f"📈 ADX: 27.3 | +DI: 28.5 > -DI: 18.2 ✅\n"
        f"💹 Above Futures-VWAP ₹24,200.00\n"
        f"🕐 {now_ist().strftime('%d %b %Y %I:%M %p')} IST\n\n"
        f"<i>NiftySignal PRO — TEST</i>"
    )
    return {"status": "Test signal sent!", "server_time_ist": now_ist().strftime('%d %b %Y %I:%M:%S %p')}

@app.get("/test-cpr")
def test_cpr():
    try:
        obj = get_angel_session()
        lines = ["📍 <b>Daily CPR Levels</b> (TEST)\n"]
        for symbol in SYMBOL_CONFIG:
            ohlc = get_previous_day_ohlc(obj, symbol)
            if not ohlc: continue
            high, low, close = ohlc; cpr = calculate_cpr(high, low, close)
            lines.append(f"\n📊 <b>{symbol}</b>\nPivot: ₹{cpr['pivot']:,}\nTC: ₹{cpr['tc']:,}  |  BC: ₹{cpr['bc']:,}")
        send_to_all("\n".join(lines)); return {"status": "Test CPR sent!"}
    except Exception as e: return {"error": str(e)}

@app.get("/debug-login")
def debug_login():
    try:
        obj = SmartConnect(api_key=API_KEY); totp = pyotp.TOTP(TOTP_KEY).now()
        data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
        return {"api_key_present": bool(API_KEY), "login_response": data}
    except Exception as e: return {"error": str(e)}

@app.get("/debug-futures")
def debug_futures():
    try:
        obj = get_angel_session(); results = {}
        for symbol in SYMBOL_CONFIG:
            token, exchange = get_futures_contract(obj, symbol)
            cached = futures_token_cache.get(symbol)
            results[symbol] = {"token": token, "exchange": exchange, "tradingsymbol": cached.get("tradingsymbol") if cached else None, "found": token is not None}
        return results
    except Exception as e: return {"error": str(e)}

@app.get("/chart-data/{symbol}/{timeframe}")
def get_chart_data(symbol: str, timeframe: str):
    symbol = symbol.upper(); timeframe = timeframe.lower()
    if symbol not in SYMBOL_CONFIG: raise HTTPException(status_code=400, detail="Use NIFTY or SENSEX")
    TIMEFRAME_MAP = {"1min": "ONE_MINUTE", "5min": "FIVE_MINUTE", "15min": "FIFTEEN_MINUTE"}
    DAYS_BACK = {"1min": 5, "5min": 14, "15min": 30}
    if timeframe not in TIMEFRAME_MAP: raise HTTPException(status_code=400, detail="Use 1min, 5min or 15min")
    config = SYMBOL_CONFIG[symbol]; interval = TIMEFRAME_MAP[timeframe]
    to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
    now = now_ist()
    if now.weekday() < 5 and now.hour >= 9: to_date = now
    from_date = to_date - timedelta(days=DAYS_BACK[timeframe])
    from_str = from_date.strftime("%Y-%m-%d %H:%M"); to_str = to_date.strftime("%Y-%m-%d %H:%M")
    try:
        obj = get_angel_session()
        response = obj.getCandleData({"exchange": config["exchange"], "symboltoken": config["token"], "interval": interval, "fromdate": from_str, "todate": to_str})
        if not response or response.get("status") == False: raise HTTPException(status_code=500, detail="Angel One returned no data")
        candles = response.get("data", [])
        if not candles: raise HTTPException(status_code=404, detail="No candle data")
        futures_candles = fetch_futures_candles(obj, symbol, interval, from_str, to_str)
        vwap_series = calculate_vwap_from_futures(candles, futures_candles)
        detect_crossover(candles, futures_candles, symbol, timeframe)
        return [[c[0], c[1], c[2], c[3], c[4], (c[5] if len(c) > 5 else 0), vwap_series[idx]] for idx, c in enumerate(candles)]
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    threading.Thread(target=signal_scanner, daemon=True).start()
    threading.Thread(target=poll_telegram,  daemon=True).start()
    threading.Thread(target=cpr_scheduler,  daemon=True).start()
    print("NiftySignal PRO started!")
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)