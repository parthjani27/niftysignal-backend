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
STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100}
LOT_SIZES = {"NIFTY": 65, "SENSEX": 25}
TRADE_BUDGET = 5000
ATM_DELTA_APPROX = 0.5  # rough ATM delta used only to ballpark option SL/target

last_signal_sent    = {}
futures_token_cache = {}
last_cpr_date       = None
ADX_THRESHOLD       = 20

IST_OFFSET = timedelta(hours=5, minutes=30)

def now_ist():
    return datetime.utcnow() + IST_OFFSET

_session_cache = {"obj": None, "logged_in_at": None}
_session_lock = threading.Lock()
SESSION_MAX_AGE = timedelta(hours=4)

def get_angel_session(force_refresh=False):
    global _session_cache
    with _session_lock:
        now = now_ist()
        if (not force_refresh and _session_cache["obj"] is not None
                and _session_cache["logged_in_at"] is not None
                and (now - _session_cache["logged_in_at"]) < SESSION_MAX_AGE):
            return _session_cache["obj"]
        try:
            obj  = SmartConnect(api_key=API_KEY)
            totp = pyotp.TOTP(TOTP_KEY).now()
            data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
            if data["status"] == False:
                raise Exception("Login failed: " + str(data))
            _session_cache["obj"] = obj
            _session_cache["logged_in_at"] = now
            print(f"Angel One session refreshed at {now.strftime('%I:%M:%S %p')} IST")
            return obj
        except Exception as e:
            raise HTTPException(status_code=401, detail=str(e))

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
                    send_to_chat(chat_id, f"✅ <b>Welcome {name}!</b>\n\nSubscribed to NiftySignal PRO!\n\n📊 Daily CPR + S/R levels at 9 AM IST\n📈 NIFTY & SENSEX signals combined in one message\n🎟 Option idea within ₹{TRADE_BUDGET:,} budget\n\n<i>NiftySignal PRO</i>")
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

def calculate_pivot_levels(high, low, close):
    pivot = (high + low + close) / 3
    r1 = 2*pivot - low;  s1 = 2*pivot - high
    r2 = pivot + (high - low); s2 = pivot - (high - low)
    r3 = high + 2*(pivot - low); s3 = low - 2*(high - pivot)
    return {k: round(v, 2) for k, v in {"pivot": pivot, "r1": r1, "r2": r2, "r3": r3, "s1": s1, "s2": s2, "s3": s3}.items()}

def find_major_levels(daily_candles, lookback=30, tolerance_pct=0.3, top_n=3):
    candles = daily_candles[-lookback:] if len(daily_candles) > lookback else daily_candles
    n = len(candles)
    if n < 3:
        return [], []
    swing_highs, swing_lows = [], []
    for i in range(1, n - 1):
        if candles[i][2] >= candles[i-1][2] and candles[i][2] >= candles[i+1][2]:
            swing_highs.append(candles[i][2])
        if candles[i][3] <= candles[i-1][3] and candles[i][3] <= candles[i+1][3]:
            swing_lows.append(candles[i][3])

    def cluster(levels):
        if not levels: return []
        levels = sorted(levels)
        clusters, current = [], [levels[0]]
        for lvl in levels[1:]:
            if abs(lvl - current[-1]) / current[-1] * 100 <= tolerance_pct:
                current.append(lvl)
            else:
                clusters.append(current); current = [lvl]
        clusters.append(current)
        result = [(round(sum(c)/len(c), 2), len(c)) for c in clusters]
        result.sort(key=lambda x: -x[1])
        return result[:top_n]

    return cluster(swing_highs), cluster(swing_lows)

def get_daily_candles(obj, symbol, lookback_days=45):
    config = SYMBOL_CONFIG[symbol]
    to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
    from_date = to_date - timedelta(days=lookback_days)
    resp = obj.getCandleData({"exchange": config["exchange"], "symboltoken": config["token"], "interval": "ONE_DAY", "fromdate": from_date.strftime("%Y-%m-%d %H:%M"), "todate": to_date.strftime("%Y-%m-%d %H:%M")})
    return resp.get("data", []) if resp else []

def get_option_contract(obj, symbol, strike, option_type):
    cfg = FUTURES_SEARCH_CONFIG.get(symbol)
    if not cfg: return None, None, None
    try:
        result = obj.searchScrip(cfg["exchange"], cfg["search"])
        scrips = result.get("data", []) if result else []
        candidates = []
        for s in scrips:
            tsym = s.get("tradingsymbol", "")
            if not tsym.endswith(option_type): continue
            if symbol == "NIFTY" and "BANK" in tsym.upper(): continue
            match = re.search(r'(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', tsym)
            if not match: continue
            exp_str, strike_str, _ = match.groups()
            try:
                expiry = datetime.strptime(exp_str, "%d%b%y")
                strike_val = float(strike_str)
            except Exception:
                continue
            candidates.append((expiry, strike_val, s.get("symboltoken"), s.get("exchange", cfg["exchange"]), tsym))
        if not candidates: return None, None, None
        candidates.sort(key=lambda x: x[0])
        nearest_expiry = candidates[0][0]
        same_expiry = [c for c in candidates if c[0] == nearest_expiry]
        same_expiry.sort(key=lambda x: abs(x[1] - strike))
        chosen = same_expiry[0]
        return chosen[2], chosen[3], chosen[4]
    except Exception as e:
        print(f"Option contract error {symbol} {strike} {option_type}: {e}")
        return None, None, None

def get_ltp(obj, exchange, tradingsymbol, token):
    try:
        data = obj.ltpData(exchange, tradingsymbol, token)
        return data.get("data", {}).get("ltp")
    except Exception as e:
        print(f"LTP fetch error: {e}")
        return None

def suggest_option_trade(obj, symbol, direction, spot_price, sl, t1, t2, t3, budget=TRADE_BUDGET):
    """
    Fetches the live ATM option premium, tells you how many lots your
    budget affords, AND ballparks an option SL/T1/T2/T3 using a rough
    ATM delta approximation (~0.5): "if the index moves X points, the
    premium moves roughly 0.5*X points." Real option movement also
    depends on gamma, theta decay, and IV changes — none of which this
    models. Treat these as a rough planning reference, verify live.
    """
    option_type = "CE" if direction == "BUY" else "PE"
    step = STRIKE_STEP.get(symbol, 50)
    atm_strike = round(spot_price / step) * step
    token, exchange, tsym = get_option_contract(obj, symbol, atm_strike, option_type)
    if not token:
        return None
    premium = get_ltp(obj, exchange, tsym, token)
    if not premium or premium <= 0:
        return None
    lot_size = LOT_SIZES.get(symbol, 1)
    cost_per_lot = premium * lot_size
    max_lots = int(budget // cost_per_lot) if cost_per_lot > 0 else 0

    move_to_sl = abs(spot_price - sl)
    move_to_t1 = abs(t1 - spot_price)
    move_to_t2 = abs(t2 - spot_price)
    move_to_t3 = abs(t3 - spot_price)

    opt_sl = round(max(premium - ATM_DELTA_APPROX * move_to_sl, 0.05), 2)
    opt_t1 = round(premium + ATM_DELTA_APPROX * move_to_t1, 2)
    opt_t2 = round(premium + ATM_DELTA_APPROX * move_to_t2, 2)
    opt_t3 = round(premium + ATM_DELTA_APPROX * move_to_t3, 2)

    return {
        "tradingsymbol": tsym, "strike": atm_strike, "option_type": option_type,
        "premium": round(premium, 2), "lot_size": lot_size,
        "cost_per_lot": round(cost_per_lot, 2), "max_lots": max_lots,
        "total_cost": round(max_lots * cost_per_lot, 2),
        "opt_sl": opt_sl, "opt_t1": opt_t1, "opt_t2": opt_t2, "opt_t3": opt_t3,
    }

def cpr_scheduler():
    global last_cpr_date
    while True:
        try:
            now = now_ist()
            today_str = now.strftime("%Y-%m-%d")
            in_window = now.weekday() < 5 and ((now.hour == 8 and now.minute >= 55) or (now.hour == 9 and now.minute <= 5))
            if in_window and last_cpr_date != today_str:
                obj = get_angel_session()
                lines = ["📍 <b>Daily Levels</b>\n<i>(Reference only — NOT a buy/sell signal)</i>\n"]
                for symbol in SYMBOL_CONFIG:
                    daily = get_daily_candles(obj, symbol)
                    if not daily or len(daily) < 2:
                        continue
                    prev = daily[-1]
                    high, low, close = prev[2], prev[3], prev[4]
                    piv = calculate_pivot_levels(high, low, close)
                    major_res, major_sup = find_major_levels(daily)
                    lines.append(
                        f"\n📊 <b>{symbol}</b>\n"
                        f"Pivot: ₹{piv['pivot']:,}\n"
                        f"R1: ₹{piv['r1']:,}  R2: ₹{piv['r2']:,}  R3: ₹{piv['r3']:,}\n"
                        f"S1: ₹{piv['s1']:,}  S2: ₹{piv['s2']:,}  S3: ₹{piv['s3']:,}\n"
                        f"<i>(Prev Day H: {high:,.2f} L: {low:,.2f} C: {close:,.2f})</i>\n"
                    )
                    if major_res:
                        lines.append("🔺 Major Resistance zones: " + ", ".join(f"₹{lvl:,.2f} ({touches}x)" for lvl, touches in major_res))
                    if major_sup:
                        lines.append("🔻 Major Support zones: " + ", ".join(f"₹{lvl:,.2f} ({touches}x)" for lvl, touches in major_sup))
                lines.append("\n\n<i>Reference only for today's rough bias. Actual BUY/SELL alerts come separately when EMA + ADX + DI + Futures-VWAP all confirm.</i>")
                send_to_all("\n".join(lines))
                last_cpr_date = today_str
                print(f"Daily levels sent for {today_str} at {now.strftime('%I:%M %p')} IST")
        except Exception as e:
            print(f"CPR error: {e}")
        time.sleep(30)

def compute_signal(spot_candles, futures_candles, symbol, timeframe):
    if len(spot_candles) < 60: return None
    closes = [c[4] for c in spot_candles]
    ema9 = calculate_ema(closes, 9); ema26 = calculate_ema(closes, 26)
    atr = calculate_atr(spot_candles, 14)
    adx, plus_di, minus_di = calculate_adx_di(spot_candles, 14)
    vwap = calculate_vwap_from_futures(spot_candles, futures_candles)
    i = len(closes) - 1
    if not ema9[i] or not ema9[i-1] or not ema26[i] or not ema26[i-1]: return None
    if atr[i] is None or adx[i] is None or vwap[i] is None: return None
    if plus_di[i] is None or minus_di[i] is None: return None
    last_time = spot_candles[-1][0]; price = closes[-1]
    is_trending = adx[i] > ADX_THRESHOLD
    bulls_lead = plus_di[i] > minus_di[i]
    bears_lead = minus_di[i] > plus_di[i]
    strength = get_signal_strength(adx[i], plus_di[i], minus_di[i])

    if ema9[i-1] <= ema26[i-1] and ema9[i] > ema26[i] and is_trending and bulls_lead and price > vwap[i]:
        return {
            "symbol": symbol, "type": "BUY", "time": last_time, "price": price,
            "sl": round(price - 1.5*atr[i], 2), "t1": round(price + 1.0*atr[i], 2),
            "t2": round(price + 2.0*atr[i], 2), "t3": round(price + 3.0*atr[i], 2),
            "strength": strength, "adx": adx[i], "plus_di": plus_di[i], "minus_di": minus_di[i], "vwap": vwap[i],
        }
    if ema9[i-1] >= ema26[i-1] and ema9[i] < ema26[i] and is_trending and bears_lead and price < vwap[i]:
        return {
            "symbol": symbol, "type": "SELL", "time": last_time, "price": price,
            "sl": round(price + 1.5*atr[i], 2), "t1": round(price - 1.0*atr[i], 2),
            "t2": round(price - 2.0*atr[i], 2), "t3": round(price - 3.0*atr[i], 2),
            "strength": strength, "adx": adx[i], "plus_di": plus_di[i], "minus_di": minus_di[i], "vwap": vwap[i],
        }
    return None

def build_combined_signal_message(obj, signals):
    if not signals: return None
    lines = []
    for sig in signals:
        emoji = "🟢" if sig["type"] == "BUY" else "🔴"
        lines.append(
            f"{emoji} <b>{sig['symbol']} {sig['type']} SIGNAL</b> — {sig['strength']}\n"
            f"💰 Price: ₹{sig['price']:,.2f}\n"
            f"🛑 SL: ₹{sig['sl']:,.2f}\n"
            f"🎯 T1: ₹{sig['t1']:,.2f}  T2: ₹{sig['t2']:,.2f}  T3: ₹{sig['t3']:,.2f}\n"
            f"📈 ADX {sig['adx']:.1f} | +DI {sig['plus_di']:.1f} vs -DI {sig['minus_di']:.1f}\n"
            f"💹 {'Above' if sig['type']=='BUY' else 'Below'} Futures-VWAP ₹{sig['vwap']:,.2f}"
        )
        opt = suggest_option_trade(obj, sig["symbol"], sig["type"], sig["price"], sig["sl"], sig["t1"], sig["t2"], sig["t3"], budget=TRADE_BUDGET)
        if opt:
            lines.append(
                f"🎟 <b>Option idea</b> (₹{TRADE_BUDGET:,} budget, reference only):\n"
                f"{opt['tradingsymbol']}\n"
                f"Premium: ₹{opt['premium']:,.2f}  |  Lot size: {opt['lot_size']}\n"
                f"Cost/lot: ₹{opt['cost_per_lot']:,.2f}  →  Affordable: <b>{opt['max_lots']} lot(s)</b> (₹{opt['total_cost']:,.2f})\n"
                f"🛑 Option SL: ₹{opt['opt_sl']:,.2f}\n"
                f"🎯 Option T1: ₹{opt['opt_t1']:,.2f}  T2: ₹{opt['opt_t2']:,.2f}  T3: ₹{opt['opt_t3']:,.2f}\n"
                f"<i>(Option SL/target are a rough ATM-delta ~0.5 estimate — real premium movement also depends on gamma, theta, and IV. Verify live before placing order.)</i>"
            )
        else:
            lines.append("🎟 <i>Option premium lookup failed — check manually on your broker app.</i>")
        lines.append("")
    lines.append("<i>⚠️ Not financial advice. Verify lot size, margin, and live premium on your broker before trading.</i>")
    lines.append(f"\n🕐 {now_ist().strftime('%d %b %Y %I:%M %p')} IST")
    return "\n".join(lines)

def signal_scanner():
    while True:
        try:
            obj = get_angel_session()
            to_date = get_last_trading_day().replace(hour=15, minute=30, second=0)
            now = now_ist()
            if now.weekday() < 5 and now.hour >= 9: to_date = now
            from_date = to_date - timedelta(days=5)
            from_str = from_date.strftime("%Y-%m-%d %H:%M"); to_str = to_date.strftime("%Y-%m-%d %H:%M")
            fired_signals = []
            for symbol, config in SYMBOL_CONFIG.items():
                try:
                    resp = obj.getCandleData({"exchange": config["exchange"], "symboltoken": config["token"], "interval": "FIVE_MINUTE", "fromdate": from_str, "todate": to_str})
                    candles = resp.get("data", [])
                    futures = fetch_futures_candles(obj, symbol, "FIVE_MINUTE", from_str, to_str)
                    if candles:
                        sig = compute_signal(candles, futures, symbol, "5min")
                        if sig:
                            key = f"{symbol}_5min_{sig['type']}_{sig['time']}"
                            if last_signal_sent.get(f"{symbol}_5min") != key:
                                last_signal_sent[f"{symbol}_5min"] = key
                                fired_signals.append(sig)
                        print(f"Scanned {symbol} — {len(candles)} candles at {now.strftime('%I:%M %p')} IST")
                except Exception as e:
                    print(f"Scanner error {symbol}: {e}")
                time.sleep(5)
            if fired_signals:
                msg = build_combined_signal_message(obj, fired_signals)
                if msg: send_to_all(msg)
        except Exception as e:
            print(f"Session error: {e}")
        time.sleep(300)

@app.get("/")
def root():
    subs = load_subscribers()
    return {"status": "NiftySignal PRO running", "subscribers": len(subs), "names": list(subs.keys()), "server_time_ist": now_ist().strftime('%d %b %Y %I:%M:%S %p'), "session_cached": _session_cache["obj"] is not None}

@app.head("/")
def root_head(): return {}

@app.get("/subscribers")
def get_subscribers(): return load_subscribers()

@app.get("/test-telegram")
def test_telegram():
    send_to_all("✅ <b>NiftySignal PRO</b>\n\nTelegram alerts working!")
    return {"status": "Sent!"}

@app.get("/test-combined-signal")
def test_combined_signal():
    try:
        obj = get_angel_session()
        fake_signals = [
            {"symbol": "NIFTY", "type": "BUY", "price": 24250.00, "sl": 24182.50, "t1": 24295.00, "t2": 24340.00, "t3": 24385.00, "strength": "STRONG 🔥", "adx": 32.1, "plus_di": 30.5, "minus_di": 14.2, "vwap": 24210.00},
            {"symbol": "SENSEX", "type": "SELL", "price": 79800.00, "sl": 79920.00, "t1": 79720.00, "t2": 79640.00, "t3": 79560.00, "strength": "MODERATE ⚡", "adx": 26.0, "plus_di": 15.0, "minus_di": 24.0, "vwap": 79850.00},
        ]
        msg = build_combined_signal_message(obj, fake_signals)
        send_to_all(msg)
        return {"status": "Test combined signal sent!"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/test-cpr")
def test_cpr():
    try:
        obj = get_angel_session()
        lines = ["📍 <b>Daily Levels</b> (TEST)\n"]
        for symbol in SYMBOL_CONFIG:
            daily = get_daily_candles(obj, symbol)
            if not daily or len(daily) < 2: continue
            prev = daily[-1]
            high, low, close = prev[2], prev[3], prev[4]
            piv = calculate_pivot_levels(high, low, close)
            major_res, major_sup = find_major_levels(daily)
            lines.append(f"\n📊 <b>{symbol}</b>\nPivot: ₹{piv['pivot']:,}\nR1: ₹{piv['r1']:,}  S1: ₹{piv['s1']:,}")
            if major_res: lines.append("Major R: " + ", ".join(f"₹{l:,.2f}({t}x)" for l,t in major_res))
            if major_sup: lines.append("Major S: " + ", ".join(f"₹{l:,.2f}({t}x)" for l,t in major_sup))
        send_to_all("\n".join(lines)); return {"status": "Test daily levels sent!"}
    except Exception as e: return {"error": str(e)}

@app.get("/debug-login")
def debug_login():
    try:
        obj = get_angel_session(force_refresh=True)
        return {"api_key_present": bool(API_KEY), "session_active": obj is not None}
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
        return [[c[0], c[1], c[2], c[3], c[4], (c[5] if len(c) > 5 else 0), vwap_series[idx]] for idx, c in enumerate(candles)]
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    threading.Thread(target=signal_scanner, daemon=True).start()
    threading.Thread(target=poll_telegram,  daemon=True).start()
    threading.Thread(target=cpr_scheduler,  daemon=True).start()
    print("NiftySignal PRO started!")
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=False)