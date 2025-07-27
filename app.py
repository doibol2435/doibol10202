from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import pandas as pd
import pandas_ta as ta
import requests
import os
import csv
from datetime import datetime
from pytz import timezone
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"

def fetch_ohlcv(symbol: str, interval: str = "15m", limit: int = 100):
    url = f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
    res = requests.get(url)
    df = pd.DataFrame(res.json(), columns=[
        "time", "open", "high", "low", "close", "volume",
        "_1", "_2", "_3", "_4", "_5", "_6"
    ])
    df["close"] = pd.to_numeric(df["close"])
    df["open"] = pd.to_numeric(df["open"])
    df["high"] = pd.to_numeric(df["high"])
    df["low"] = pd.to_numeric(df["low"])
    return df

def analyze(df: pd.DataFrame):
    df["RSI"] = ta.rsi(df["close"], length=14)
    stoch = ta.stoch(df["high"], df["low"], df["close"])
    df["%K"] = stoch["STOCHk_14_3_3"]
    df["%D"] = stoch["STOCHd_14_3_3"]
    macd = ta.macd(df["close"])
    df["MACD"] = macd["MACD_12_26_9"]
    df["MACD_signal"] = macd["MACDs_12_26_9"]
    bb = ta.bbands(df["close"], length=20)
    df["BB_lower"] = bb["BBL_20_2.0"]
    df["BB_upper"] = bb["BBU_20_2.0"]
    df.dropna(inplace=True)
    if df.empty or len(df) < 2:
        return None

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    rsi_buy = curr["RSI"] < 30 and curr["RSI"] > prev["RSI"]
    rsi_sell = curr["RSI"] > 70 and curr["RSI"] < prev["RSI"]

    stoch_buy = curr["%K"] > curr["%D"] and prev["%K"] < prev["%D"] and curr["%K"] < 20
    stoch_sell = curr["%K"] < curr["%D"] and prev["%K"] > prev["%D"] and curr["%K"] > 80

    macd_buy = curr["MACD"] > curr["MACD_signal"] and prev["MACD"] < prev["MACD_signal"] and curr["MACD"] > 0
    macd_sell = curr["MACD"] < curr["MACD_signal"] and prev["MACD"] > prev["MACD_signal"] and curr["MACD"] < 0

    bb_buy = curr["close"] < prev["BB_lower"] and curr["close"] > curr["BB_lower"]
    bb_sell = curr["close"] > prev["BB_upper"] and curr["close"] < curr["BB_upper"]

    score_buy = int(rsi_buy + stoch_buy + macd_buy + bb_buy)
    score_sell = int(rsi_sell + stoch_sell + macd_sell + bb_sell)

    decision = "Hold"
    if score_buy >= 2:
        decision = "Buy"
    elif score_sell >= 2:
        decision = "Sell"

    return {
        "rsi": float(curr["RSI"]),
        "macd": float(curr["MACD"]),
        "signal": float(curr["MACD_signal"]),
        "price": float(curr["close"]),
        "decision": decision,
        "score_buy": score_buy,
        "score_sell": score_sell,
        "timestamp": datetime.now(timezone("Asia/Ho_Chi_Minh")).isoformat()
    }

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    requests.post(url, data=payload)

def log_signal(symbol, direction, entry, tp1, tp2, tp3, sl, timestamp=None):
    with open("log.csv", mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            timestamp or datetime.now(timezone("Asia/Ho_Chi_Minh")).isoformat(),
            symbol, direction, entry, tp1, tp2, tp3, sl
        ])

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/scan")
def scan_top_futures():
    try:
        res = requests.get(BINANCE_INFO).json()
        symbols = [s['symbol'] for s in res['symbols'] if s['quoteAsset'] == 'USDT' and s['contractType'] == 'PERPETUAL'][:200]
        signals = []
        for symbol in symbols:
            try:
                df = fetch_ohlcv(symbol)
                result = analyze(df)
                if result:
                    signals.append({"symbol": symbol, **result})
                    if result["decision"] in ["Buy", "Sell"]:
                        entry = result["price"]
                        tp1 = entry * 1.02
                        tp2 = entry * 1.04
                        tp3 = entry * 1.06
                        sl = entry * 0.98
                        msg = f"""{result['decision']} Signal: {symbol}
Entry: {entry}
TP1: {tp1} | TP2: {tp2} | TP3: {tp3}
SL: {sl}"""
                        send_telegram(msg)
                        log_signal(symbol, result["decision"], entry, tp1, tp2, tp3, sl, result["timestamp"])
            except:
                continue
        return {"results": signals, "count": len(signals)}
    except Exception as e:
        return {"error": str(e)}

# Auto scan scheduler
def auto_scan():
    try:
        requests.get("http://localhost:8000/scan")
    except:
        pass

scheduler = BackgroundScheduler()
scheduler.add_job(auto_scan, "interval", minutes=5)
scheduler.start()
