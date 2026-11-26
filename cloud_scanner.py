import json
import os
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from nepsepy import NepseClient

# ============================================================
# CONFIGURATION
# ============================================================
TZ = ZoneInfo("Asia/Kathmandu")
MARKET_START = dt_time(11, 0)
MARKET_END = dt_time(15, 0)

EMA_SHORT = 7
EMA_LONG = 21
HISTORY_SIZE = 100

SCAN_INTERVAL_SECONDS = 300
REQUEST_DELAY_SECONDS = 0.35

ROOT = Path(__file__).resolve().parent
STOCK_FILE = ROOT / "filtered_stocks.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def now_local():
    return datetime.now(TZ)


def load_stocks():
    with STOCK_FILE.open("r", encoding="utf-8") as f:
        stocks = json.load(f)
    if not isinstance(stocks, list):
        raise ValueError("filtered_stocks.json must contain a JSON list.")
    print(f"Filtered candidates loaded: {len(stocks)}")
    return stocks


def get_symbol(record):
    security = record.get("security") or {}
    return str(record.get("symbol") or security.get("symbol") or "").upper()


def get_security_id(record):
    security = record.get("security") or {}
    return (
        record.get("securityId")
        or record.get("security_id")
        or security.get("id")
        or record.get("nepse_id")
    )


def get_price(record):
    # NEPSE today's-price records commonly expose lastTradedPrice.
    for key in ("lastTradedPrice", "last_traded_price", "ltp", "closePrice"):
        value = record.get(key)
        if value not in (None, "", 0, "0"):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def get_today_market_data(client):
    """
    Fetch the NEPSE today's-price list once per scan cycle.
    It supplies the latest available traded price and, where present,
    the security ID needed for historical daily candles.
    """
    raw = client.today_price_all()
    by_symbol = {}

    for row in raw or []:
        symbol = get_symbol(row)
        if not symbol:
            continue
        by_symbol[symbol] = {
            "security_id": get_security_id(row),
            "price": get_price(row),
        }
    return by_symbol


def get_previous_completed_emas(client, security_id, today):
    history = client.security_price_history(
        security_id=int(security_id),
        page=1,
        size=HISTORY_SIZE,
    )
    records = history.get("content", []) if isinstance(history, dict) else []
    if not records:
        return None

    df = pd.DataFrame(records)
    if "businessDate" not in df or "closePrice" not in df:
        return None

    df["date"] = pd.to_datetime(df["businessDate"], errors="coerce")
    df["close"] = pd.to_numeric(df["closePrice"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df.drop_duplicates(subset=["date"]).sort_values("date")
    df = df[df["date"].dt.date < today]

    if len(df) < EMA_LONG + 1:
        return None

    ema7 = df["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema21 = df["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]

    return {
        "date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "ema7": float(ema7),
        "ema21": float(ema21),
    }


def calculate_signal(live_price, previous):
    alpha7 = 2 / (EMA_SHORT + 1)
    alpha21 = 2 / (EMA_LONG + 1)

    today_ema7 = live_price * alpha7 + previous["ema7"] * (1 - alpha7)
    today_ema21 = live_price * alpha21 + previous["ema21"] * (1 - alpha21)

    signal = ""
    if previous["ema7"] <= previous["ema21"] and today_ema7 > today_ema21:
        signal = "BUY"
    elif previous["ema7"] >= previous["ema21"] and today_ema7 < today_ema21:
        signal = "SELL"

    return today_ema7, today_ema21, signal


def send_telegram(symbol, signal, price, ema7, ema21):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing from GitHub Actions.")

    message = (
        f"📊 NEPSE DAILY EMA 7/21 {signal}\n\n"
        f"Symbol: {symbol}\n"
        f"Latest price: {price:.2f}\n"
        f"Provisional EMA 7: {ema7:.2f}\n"
        f"Provisional EMA 21: {ema21:.2f}\n"
        f"Time: {now_local():%Y-%m-%d %H:%M:%S} NPT\n\n"
        "Note: Intraday provisional crossover; it can reverse before market close."
    )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")


def main():
    print("=" * 58)
    print(" NEPSE CLOUD DAILY EMA 7/21 CROSSOVER MONITOR")
    print(" Historical daily candles + today's latest traded price")
    print(" Runs weekdays, 11:00 AM to 3:00 PM Nepal time")
    print("=" * 58)

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in repository Secrets.")

    stocks = load_stocks()
    ema_cache = {}
    sent_today = set()
    cache_date = None

    with NepseClient() as client:
        while True:
            now = now_local()

            if now.weekday() >= 5:
                print("Weekend in Nepal. Exiting.")
                return

            if now.time() < MARKET_START:
                opening = datetime.combine(now.date(), MARKET_START, tzinfo=TZ)
                seconds = max(1, (opening - now).total_seconds())
                print(f"Before market open. Waiting {seconds / 60:.1f} minutes.")
                time.sleep(seconds)
                continue

            if now.time() >= MARKET_END:
                print("Market session ended. Exiting.")
                return

            today = now.date()
            today_text = today.isoformat()

            if cache_date != today_text:
                print("Preparing previous completed daily EMA values...")
                ema_cache = {}
                market_data = get_today_market_data(client)

                for index, stock in enumerate(stocks, start=1):
                    symbol = str(stock.get("symbol", "")).upper()
                    row = market_data.get(symbol, {})
                    security_id = (
                        row.get("security_id")
                        or stock.get("securityId")
                        or stock.get("security_id")
                        or stock.get("nepse_id")
                    )
                    if not symbol or not security_id:
                        print(f"[{index}/{len(stocks)}] {symbol}: no security ID")
                        continue
                    try:
                        values = get_previous_completed_emas(client, security_id, today)
                        if values:
                            ema_cache[symbol] = values
                        else:
                            print(f"[{index}/{len(stocks)}] {symbol}: no usable daily history")
                    except Exception as exc:
                        print(f"[{index}/{len(stocks)}] {symbol}: history error: {exc}")
                    time.sleep(REQUEST_DELAY_SECONDS)

                cache_date = today_text
                print(f"Historical EMA cache ready: {len(ema_cache)} stocks")

            scan_started = time.time()
            market_data = get_today_market_data(client)
            successful = failed = signals_found = alerts_sent = 0

            print(f"\nScan started: {now_local():%H:%M:%S} NPT")
            for index, stock in enumerate(stocks, start=1):
                symbol = str(stock.get("symbol", "")).upper()
                previous = ema_cache.get(symbol)
                row = market_data.get(symbol, {})
                price = row.get("price")

                if not previous or price is None:
                    failed += 1
                    continue

                successful += 1
                ema7, ema21, signal = calculate_signal(float(price), previous)

                print(
                    f"[{index}/{len(stocks)}] {symbol}: "
                    f"Price={float(price):.2f} | EMA7={ema7:.2f} | "
                    f"EMA21={ema21:.2f} | Signal={signal or '-'}"
                )

                if not signal:
                    continue

                signals_found += 1
                alert_id = f"{today_text}:{symbol}:{signal}"
                if alert_id in sent_today:
                    continue

                try:
                    send_telegram(symbol, signal, float(price), ema7, ema21)
                    sent_today.add(alert_id)
                    alerts_sent += 1
                    print(f"  Telegram {signal} alert sent: {symbol}")
                except Exception as exc:
                    print(f"  Telegram alert failed for {symbol}: {exc}")

            elapsed = time.time() - scan_started
            print(
                f"Scan complete | prices: {successful} | missing: {failed} | "
                f"crossover conditions: {signals_found} | alerts sent: {alerts_sent} | "
                f"elapsed: {elapsed:.1f}s"
            )

            remaining = SCAN_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                print(f"Next scan in about {remaining:.0f} seconds.")
                time.sleep(remaining)


if __name__ == "__main__":
    main()
