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

# Print the actual API response once for diagnosis
PRICE_DEBUG_PRINTED = False


# ============================================================
# TIME
# ============================================================

def now_local():
    return datetime.now(TZ)


# ============================================================
# LOAD STOCKS
# ============================================================

def load_stocks():

    with STOCK_FILE.open("r", encoding="utf-8") as f:
        stocks = json.load(f)

    if not isinstance(stocks, list):
        raise ValueError(
            "filtered_stocks.json must contain a JSON list."
        )

    print(f"Filtered candidates loaded: {len(stocks)}")

    return stocks


# ============================================================
# SYMBOL EXTRACTION
# ============================================================

def get_symbol(record):

    if not isinstance(record, dict):
        return ""

    security = record.get("security") or {}

    possible_symbols = [
        record.get("symbol"),
        record.get("stockSymbol"),
        record.get("securitySymbol"),
        record.get("ticker"),
        record.get("stock_symbol"),
        security.get("symbol"),
        security.get("stockSymbol"),
        security.get("securitySymbol"),
    ]

    for value in possible_symbols:

        if value not in (None, ""):

            return str(value).strip().upper()

    return ""


# ============================================================
# SECURITY ID
# ============================================================

def get_security_id(record):

    if not isinstance(record, dict):
        return None

    security = record.get("security") or {}

    possible_ids = [
        record.get("securityId"),
        record.get("security_id"),
        record.get("id"),
        record.get("nepse_id"),
        security.get("id"),
        security.get("securityId"),
        security.get("security_id"),
    ]

    for value in possible_ids:

        if value not in (None, ""):

            return value

    return None


# ============================================================
# PRICE EXTRACTION
# ============================================================

def get_price(record):

    if not isinstance(record, dict):
        return None

    possible_prices = [
        "lastTradedPrice",
        "last_traded_price",
        "ltp",
        "closePrice",
        "close_price",
        "currentPrice",
        "current_price",
        "price",
        "lastPrice",
        "last_price",
    ]

    for key in possible_prices:

        value = record.get(key)

        if value in (None, "", 0, "0"):
            continue

        try:

            return float(value)

        except (TypeError, ValueError):

            continue

    return None


# ============================================================
# TODAY PRICE DATA
# ============================================================

def get_today_market_data(client):

    global PRICE_DEBUG_PRINTED

    print("\nFetching today's NEPSE price data...")

    try:

        raw = client.today_price_all()

    except Exception as exc:

        print(
            "today_price_all() failed:",
            repr(exc)
        )

        return {}

    print(
        "Today's price response type:",
        type(raw).__name__
    )

    # --------------------------------------------------------
    # HANDLE RESPONSE STRUCTURE
    # --------------------------------------------------------

    if isinstance(raw, dict):

        print(
            "Today's price response keys:",
            list(raw.keys())
        )

        rows = (
            raw.get("content")
            or raw.get("data")
            or raw.get("records")
            or raw.get("result")
            or raw.get("prices")
            or []
        )

    elif isinstance(raw, list):

        rows = raw

    else:

        print(
            "Unexpected response structure:"
        )

        print(
            str(raw)[:2000]
        )

        return {}

    # --------------------------------------------------------
    # PRINT FIRST RESPONSE FOR DIAGNOSIS
    # --------------------------------------------------------

    if not PRICE_DEBUG_PRINTED:

        print("\n========== PRICE API DEBUG ==========")

        print(
            "Number of records returned:",
            len(rows)
        )

        if rows:

            print("First record:")

            print(
                json.dumps(
                    rows[0],
                    indent=2,
                    default=str
                )[:3000]
            )

            if isinstance(rows[0], dict):

                print(
                    "First record keys:",
                    list(rows[0].keys())
                )

        else:

            print(
                "API returned no price records."
            )

        print("===================================\n")

        PRICE_DEBUG_PRINTED = True

    # --------------------------------------------------------
    # BUILD SYMBOL LOOKUP
    # --------------------------------------------------------

    by_symbol = {}

    for row in rows:

        if not isinstance(row, dict):
            continue

        symbol = get_symbol(row)

        if not symbol:
            continue

        price = get_price(row)

        security_id = get_security_id(row)

        by_symbol[symbol] = {
            "security_id": security_id,
            "price": price,
        }

    print(
        "Symbols matched from price response:",
        len(by_symbol)
    )

    if by_symbol:

        sample_symbols = list(by_symbol.keys())[:10]

        print(
            "Sample symbols:",
            sample_symbols
        )

    return by_symbol


# ============================================================
# HISTORICAL DAILY EMA
# ============================================================

def get_previous_completed_emas(
    client,
    security_id,
    today
):

    history = client.security_price_history(
        security_id=int(security_id),
        page=1,
        size=HISTORY_SIZE,
    )

    records = (
        history.get("content", [])
        if isinstance(history, dict)
        else []
    )

    if not records:
        return None

    df = pd.DataFrame(records)

    if (
        "businessDate" not in df
        or "closePrice" not in df
    ):
        return None

    df["date"] = pd.to_datetime(
        df["businessDate"],
        errors="coerce"
    )

    df["close"] = pd.to_numeric(
        df["closePrice"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["date", "close"]
    )

    df = df.drop_duplicates(
        subset=["date"]
    )

    df = df.sort_values("date")

    # Use only completed daily candles
    df = df[
        df["date"].dt.date < today
    ]

    if len(df) < EMA_LONG + 1:
        return None

    ema7 = (
        df["close"]
        .ewm(
            span=EMA_SHORT,
            adjust=False
        )
        .mean()
        .iloc[-1]
    )

    ema21 = (
        df["close"]
        .ewm(
            span=EMA_LONG,
            adjust=False
        )
        .mean()
        .iloc[-1]
    )

    return {
        "date": df["date"].iloc[-1].strftime(
            "%Y-%m-%d"
        ),
        "ema7": float(ema7),
        "ema21": float(ema21),
    }


# ============================================================
# CALCULATE PROVISIONAL EMA SIGNAL
# ============================================================

def calculate_signal(
    live_price,
    previous
):

    alpha7 = 2 / (EMA_SHORT + 1)

    alpha21 = 2 / (EMA_LONG + 1)

    today_ema7 = (
        live_price * alpha7
        + previous["ema7"] * (1 - alpha7)
    )

    today_ema21 = (
        live_price * alpha21
        + previous["ema21"] * (1 - alpha21)
    )

    signal = ""

    if (
        previous["ema7"] <= previous["ema21"]
        and today_ema7 > today_ema21
    ):

        signal = "BUY"

    elif (
        previous["ema7"] >= previous["ema21"]
        and today_ema7 < today_ema21
    ):

        signal = "SELL"

    return (
        today_ema7,
        today_ema21,
        signal
    )


# ============================================================
# TELEGRAM ALERT
# ============================================================

def send_telegram(
    symbol,
    signal,
    price,
    ema7,
    ema21
):

    if not BOT_TOKEN or not CHAT_ID:

        raise RuntimeError(
            "Telegram secrets are missing "
            "from GitHub Actions."
        )

    message = (
        f"📊 NEPSE DAILY EMA 7/21 {signal}\n\n"
        f"Symbol: {symbol}\n"
        f"Latest price: {price:.2f}\n"
        f"Provisional EMA 7: {ema7:.2f}\n"
        f"Provisional EMA 21: {ema21:.2f}\n"
        f"Time: {now_local():%Y-%m-%d %H:%M:%S} NPT\n\n"
        "Note: Intraday provisional crossover; "
        "it can reverse before market close."
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):

        raise RuntimeError(
            f"Telegram API error: {data}"
        )


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    print("=" * 58)

    print(
        " NEPSE CLOUD DAILY EMA 7/21 CROSSOVER MONITOR"
    )

    print(
        " Historical daily candles + today's latest traded price"
    )

    print(
        " Runs weekdays, 11:00 AM to 3:00 PM Nepal time"
    )

    print("=" * 58)

    if not BOT_TOKEN or not CHAT_ID:

        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in repository Secrets."
        )

    stocks = load_stocks()

    ema_cache = {}

    sent_today = set()

    cache_date = None

    with NepseClient() as client:

        while True:

            now = now_local()

            # ------------------------------------------------
            # WEEKEND CHECK
            # ------------------------------------------------

            if now.weekday() >= 5:

                print(
                    "Weekend in Nepal. Exiting."
                )

                return

            # ------------------------------------------------
            # BEFORE MARKET OPEN
            # ------------------------------------------------

            if now.time() < MARKET_START:

                opening = datetime.combine(
                    now.date(),
                    MARKET_START,
                    tzinfo=TZ
                )

                seconds = max(
                    1,
                    (opening - now).total_seconds()
                )

                print(
                    f"Before market open. "
                    f"Waiting {seconds / 60:.1f} minutes."
                )

                time.sleep(seconds)

                continue

            # ------------------------------------------------
            # AFTER MARKET CLOSE
            # ------------------------------------------------

            if now.time() >= MARKET_END:

                print(
                    "Market session ended. Exiting."
                )

                return

            today = now.date()

            today_text = today.isoformat()

            # ------------------------------------------------
            # PREPARE HISTORICAL EMA CACHE
            # ------------------------------------------------

            if cache_date != today_text:

                print(
                    "Preparing previous completed daily EMA values..."
                )

                ema_cache = {}

                market_data = get_today_market_data(
                    client
                )

                for index, stock in enumerate(
                    stocks,
                    start=1
                ):

                    symbol = str(
                        stock.get("symbol", "")
                    ).upper()

                    row = market_data.get(
                        symbol,
                        {}
                    )

                    security_id = (
                        row.get("security_id")
                        or stock.get("securityId")
                        or stock.get("security_id")
                        or stock.get("nepse_id")
                    )

                    if not symbol or not security_id:

                        print(
                            f"[{index}/{len(stocks)}] "
                            f"{symbol}: no security ID"
                        )

                        continue

                    try:

                        values = (
                            get_previous_completed_emas(
                                client,
                                security_id,
                                today
                            )
                        )

                        if values:

                            ema_cache[symbol] = values

                        else:

                            print(
                                f"[{index}/{len(stocks)}] "
                                f"{symbol}: no usable daily history"
                            )

                    except Exception as exc:

                        print(
                            f"[{index}/{len(stocks)}] "
                            f"{symbol}: history error: {exc}"
                        )

                    time.sleep(
                        REQUEST_DELAY_SECONDS
                    )

                cache_date = today_text

                print(
                    f"Historical EMA cache ready: "
                    f"{len(ema_cache)} stocks"
                )

            # ------------------------------------------------
            # CURRENT PRICE SCAN
            # ------------------------------------------------

            scan_started = time.time()

            market_data = get_today_market_data(
                client
            )

            successful = 0

            failed = 0

            signals_found = 0

            alerts_sent = 0

            print(
                f"\nScan started: "
                f"{now_local():%H:%M:%S} NPT"
            )

            for index, stock in enumerate(
                stocks,
                start=1
            ):

                symbol = str(
                    stock.get("symbol", "")
                ).upper()

                previous = ema_cache.get(
                    symbol
                )

                row = market_data.get(
                    symbol,
                    {}
                )

                price = row.get(
                    "price"
                )

                if not previous or price is None:

                    failed += 1

                    continue

                successful += 1

                ema7, ema21, signal = (
                    calculate_signal(
                        float(price),
                        previous
                    )
                )

                print(
                    f"[{index}/{len(stocks)}] "
                    f"{symbol}: "
                    f"Price={float(price):.2f} | "
                    f"EMA7={ema7:.2f} | "
                    f"EMA21={ema21:.2f} | "
                    f"Signal={signal or '-'}"
                )

                if not signal:
                    continue

                signals_found += 1

                alert_id = (
                    f"{today_text}:{symbol}:{signal}"
                )

                if alert_id in sent_today:
                    continue

                try:

                    send_telegram(
                        symbol,
                        signal,
                        float(price),
                        ema7,
                        ema21
                    )

                    sent_today.add(
                        alert_id
                    )

                    alerts_sent += 1

                    print(
                        f"Telegram {signal} alert sent: "
                        f"{symbol}"
                    )

                except Exception as exc:

                    print(
                        f"Telegram alert failed for "
                        f"{symbol}: {exc}"
                    )

            # ------------------------------------------------
            # SCAN SUMMARY
            # ------------------------------------------------

            elapsed = (
                time.time() - scan_started
            )

            print(
                f"Scan complete | "
                f"prices: {successful} | "
                f"missing: {failed} | "
                f"crossover conditions: {signals_found} | "
                f"alerts sent: {alerts_sent} | "
                f"elapsed: {elapsed:.1f}s"
            )

            # ------------------------------------------------
            # WAIT FOR NEXT SCAN
            # ------------------------------------------------

            remaining = (
                SCAN_INTERVAL_SECONDS - elapsed
            )

            if remaining > 0:

                print(
                    f"Next scan in about "
                    f"{remaining:.0f} seconds."
                )

                time.sleep(
                    remaining
                )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
