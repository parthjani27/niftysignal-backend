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

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY   = "pBBfHTNf"
CLIENT_ID   = "MAEL1045"       # e.g. "A12345678"
PASSWORD    = "2911"         # your Angel One login password
TOTP_KEY    = "7Y26PZSOTY54CNCAMFQZO7XGPI"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8935646874:AAGhni4W84AwNmcm5YM9WMTSs16NpfHcdxU")
SUBSCRIBERS_FILE = "subscribers.json"

SYMBOL_CONFIG = {
    "NIFTY":  {"token": "99926000", "exchange": "NSE"},
    "SENSEX": {"token": "99919000", "exchange": "BSE"},
}
last_signal_sent = {}

ADX_THRESHOLD = 20

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
    today = datetime.now()
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
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def send_to_all(message: str):
    subs = load_subscribers()
    for name, chat_id in subs.items():
        send_to_chat(chat_id, message)
        print(f"Sent to {name} ({chat_id})")

def poll_telegram():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
            data = resp.json()
            for update in data.get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                name    = msg.get("chat", {}).get("first_name", "User")
                if text.startswith("/start"):
                    subs = load_subscribers()
                    subs[name] = chat_id
                    save_subscribers(subs)
                    subscribers[name] = chat_id
                    send_to_chat(chat_id,
                        f"✅ <b>Welcome {name}!</b>\n\n"
                        f"You are now subscribed to NiftySignal PRO!\n\n"
                        f"You will receive BUY/SELL signals for:\n"
                        f"📊 NIFTY 50 & SENSEX\n"
                        f"📈 EMA 9 × EMA 26 crossover + ADX + VWAP confirmed\n"
                        f"⏱ 5 minute chart\n\n"
                        f"Your Chat ID: <code>{chat_id}</code>\n\n"
                        f"<i>NiftySignal PRO</i>"
                    )
                    print(f"New subscriber: {name} ({chat_id})")
                elif text.startswith("/stop"):
                    subs = load_subscribers()
                    if name in subs:
                        del subs[name]
                        save_subscribers(subs)
                        subscribers.pop(name, None)
                    send_to_chat(chat_id, "❌ You have been unsubscribed from NiftySignal PRO.")
                elif text.startswith("/status"):
                    send_to_chat(chat_id,
                        f"📊 <b>NiftySignal PRO Status</b>\n\n"
                        f"✅ Bot is running\n"
                        f"👤 Subscribers: {len(load_subscribers())}\n"
                        f"🕐 Time: {datetime.now().strftime('%d %b %Y %I:%M %p')}"
                    )
        except Exception as e:
            print(f"Telegram poll error: {e}")
        time.sleep(2)

def calculate_ema(prices, period):
    if len(prices) < period:
        return []
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
    if n < period + 1:
        return atr
    tr = [None] * n
    for i in range(1, n):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i-1][4]
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    first_atr = sum(tr[1:period+1]) / period
    atr[period] = first_atr
    for i in range(period + 1, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

def calculate_adx(candles, period=14):
    n = len(candles)
    adx = [None] * n
    if n < period * 2:
        return adx
    plus_dm  = [0] * n
    minus_dm = [0] * n
    tr       = [0] * n
    for i in range(1, n):
        high, low = candles[i][2], candles[i][3]
        prev_high, prev_low, prev_close = candles[i-1][2], candles[i-1][3], candles[i-1][4]
        up_move   = high - prev_high
        down_move = prev_low - low
        plus_dm[i]  = up_move   if (up_move > down_move and up_move > 0) else 0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    sm_tr = [None] * n
    sm_plus = [None] * n
    sm_minus = [None] * n
    sm_tr[period]    = sum(tr[1:period+1])
    sm_plus[period]  = sum(plus_dm[1:period+1])
    sm_minus[period] = sum(minus_dm[1:period+1])
    for i in range(period + 1, n):
        sm_tr[i]    = sm_tr[i-1]    - (sm_tr[i-1] / period)    + tr[i]
        sm_plus[i]  = sm_plus[i-1]  - (sm_plus[i-1] / period)  + plus_dm[i]
        sm_minus[i] = sm_minus[i-1] - (sm_minus[i-1] / period) + minus_dm[i]

    dx = [None] * n
    for i in range(period, n):
        if not sm_tr[i]:
            continue
        plus_di  = 100 * (sm_plus[i] / sm_tr[i])
        minus_di = 100 * (sm_minus[i] / sm_tr[i])
        if (plus_di + minus_di) == 0:
            continue
        dx[i] = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)

    valid = [d for d in dx[period:period*2] if d is not None]
    if len(valid) < period:
        return adx
    adx[period*2 - 1] = sum(valid) / period
    for i in range(period*2, n):
        if dx[i] is None:
            continue
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
    return adx

def calculate_vwap(candles):
    n = len(candles)
    vwap = [None] * n
    cum_pv, cum_vol = 0, 0
    current_day = None
    for i in range(n):
        day = str(candles[i][0])[:10]
        if day != current_day:
            current_day = day
            cum_pv, cum_vol = 0, 0
        typical_price = (candles[i][2] + candles[i][3] + candles[i][4]) / 3
        volume = candles[i][5] if len(candles[i]) > 5 else 0
        cum_pv  += typical_price * volume
        cum_vol += volume
        vwap[i] = cum_pv / cum_vol if cum_vol > 0 else candles[i][4]
    return vwap

def detect_crossover(candles, symbol, timeframe):
    if len(candles) < 60:
        return
    closes = [c[4] for c in candles]
    ema9  = calculate_ema(closes, 9)
    ema26 = calculate_ema(closes, 26)
    atr   = calculate_atr(candles, 14)
    adx   = calculate_adx(candles, 14)
    vwap  = calculate_vwap(candles)
    i = len(closes) - 1
    if not ema9[i] or not ema9[i-1] or not ema26[i] or not ema26[i-1]:
        return
    if atr[i] is None or adx[i] is None or vwap[i] is None:
        return
    signal_key = f"{symbol}_{timeframe}"
    last_time  = candles[-1][0]
    price      = closes[-1]
    is_trending = adx[i] > ADX_THRESHOLD

    if ema9[i-1] <= ema26[i-1] and ema9[i] > ema26[i]:
        if is_trending and price > vwap[i]:
            key = f"{signal_key}_BUY_{last_time}"
            if last_signal_sent.get(signal_key) != key:
                last_signal_sent[signal_key] = key
                sl     = round(price - (1.5 * atr[i]), 2)
                target = round(price + (2 * atr[i]), 2)
                msg = (
                    f"🟢 <b>BUY SIGNAL</b>\n\n"
                    f"📊 <b>{symbol}</b> | {timeframe}\n"
                    f"💰 Price: <b>₹{price:,.2f}</b>\n"
                    f"🛑 SL: ₹{sl:,.2f}  🎯 Target: ₹{target:,.2f}\n"
                    f"📈 ADX: {adx[i]:.1f} (trending)  |  Above VWAP ₹{vwap[i]:,.2f}\n"
                    f"🕐 {datetime.now().strftime('%d %b %Y %I:%M %p')}\n\n"
                    f"<i>NiftySignal PRO</i>"
                )
                send_to_all(msg)
    elif ema9[i-1] >= ema26[i-1] and ema9[i] < ema26[i]:
        if is_trending and price < vwap[i]:
            key = f"{signal_key}_SELL_{last_time}"
            if last_signal_sent.get(signal_key) != key:
                last_signal_sent[signal_key] = key
                sl     = round(price + (1.5 * atr[i]), 2)
                target = round(price - (2 * atr[i]), 2)
                msg = (
                    f"🔴 <b>SELL SIGNAL</b>\n\n"
                    f"📊 <b>{symbol}</b> | {timeframe}\n"
                    f"💰 Price: <b>₹{price:,.2f}</b>\n"
                    f"🛑 SL: ₹{sl:,.2f}  🎯 Target: ₹{target:,.2f}\n"
                    f"📉 ADX: {adx[i]:.1f} (trending)  |  Below VWAP ₹{vwap[i]:,.2f}\n"
                    f"🕐 {datetime.now().strftime('%d %b %Y %I:%M %p')}\n\n"
                    f"<i>NiftySignal PRO</i>"
                )
                send_to_all(msg)

def signal_scanner():
    while True:
        try:
            obj     = get_angel_session()
            to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
            now     = datetime.now()
            if now.weekday() < 5 and now.hour >= 9:
                to_date = now
            from_date = to_date - timedelta(days=5)
            from_str  = from_date.strftime("%Y-%m-%d %H:%M")
            to_str    = to_date.strftime("%Y-%m-%d %H:%M")
            for symbol, config in SYMBOL_CONFIG.items():
                try:
                    resp = obj.getCandleData({
                        "exchange":    config["exchange"],
                        "symboltoken": config["token"],
                        "interval":    "FIVE_MINUTE",
                        "fromdate":    from_str,
                        "todate":      to_str,
                    })
                    candles = resp.get("data", [])
                    if candles:
                        detect_crossover(candles, symbol, "5min")
                        print(f"Scanner checked {symbol} — {len(candles)} candles")
                except Exception as e:
                    print(f"Scanner error {symbol}: {e}")
                time.sleep(5)
        except Exception as e:
            print(f"Session error: {e}")
        time.sleep(300)

@app.get("/")
def root():
    subs = load_subscribers()
    return {"status": "NiftySignal running", "subscribers": len(subs), "names": list(subs.keys())}

@app.get("/subscribers")
def get_subscribers():
    return load_subscribers()

@app.get("/test-telegram")
def test_telegram():
    send_to_all("✅ <b>NiftySignal PRO</b>\n\nTelegram alerts working!")
    return {"status": "Sent to all subscribers!"}

@app.get("/chart-data/{symbol}/{timeframe}")
def get_chart_data(symbol: str, timeframe: str):
    symbol    = symbol.upper()
    timeframe = timeframe.lower()
    if symbol not in SYMBOL_CONFIG:
        raise HTTPException(status_code=400, detail="Use NIFTY or SENSEX")
    TIMEFRAME_MAP = {"1min": "ONE_MINUTE", "5min": "FIVE_MINUTE", "15min": "FIFTEEN_MINUTE"}
    DAYS_BACK = {"1min": 5, "5min": 14, "15min": 30}
    if timeframe not in TIMEFRAME_MAP:
        raise HTTPException(status_code=400, detail="Use 1min, 5min or 15min")
    config    = SYMBOL_CONFIG[symbol]
    interval  = TIMEFRAME_MAP[timeframe]
    to_date   = get_last_trading_day().replace(hour=15, minute=30, second=0)
    now       = datetime.now()
    if now.weekday() < 5 and now.hour >= 9:
        to_date = now
    from_date = to_date - timedelta(days=DAYS_BACK[timeframe])
    try:
        obj      = get_angel_session()
        response = obj.getCandleData({
            "exchange":    config["exchange"],
            "symboltoken": config["token"],
            "interval":    interval,
            "fromdate":    from_date.strftime("%Y-%m-%d %H:%M"),
            "todate":      to_date.strftime("%Y-%m-%d %H:%M"),
        })
        if not response or response.get("status") == False:
            raise HTTPException(status_code=500, detail="Angel One returned no data")
        candles = response.get("data", [])
        if not candles:
            raise HTTPException(status_code=404, detail="No candle data")
        detect_crossover(candles, symbol, timeframe)
        return [[c[0], c[1], c[2], c[3], c[4], (c[5] if len(c) > 5 else 0)] for c in candles]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    threading.Thread(target=signal_scanner, daemon=True).start()
    threading.Thread(target=poll_telegram, daemon=True).start()
    print("Signal scanner started!")
    print("Telegram bot polling started!")
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)