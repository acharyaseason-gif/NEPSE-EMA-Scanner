import os
import json
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
SCAN_INTERVAL = 300

BASE_DIR = Path(__file__).resolve().parent

STOCK_FILE = BASE_DIR / "filtered_stocks.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PRICE_DEBUG_PRINTED = False


# ============================================================
# TIME FUNCTIONS
# ============================================================

def now_nepal():
    return datetime.now(TZ)


def is_weekday():
    return now_nepal().weekday() < 5


def is_market_open():
    current_time = now_nepal().time()

    return (
        is_weekday()
        and MARKET_START <= current_time <= MARKET_END
    )


def print_time():
    return now_nepal().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# LOAD FILTERED STOCKS
# ============================================================

def load_candidates():

    if not STOCK_FILE.exists():
        raise FileNotFoundError(
            f"Stock file not found: {STOCK_FILE}"
        )

    with open(STOCK_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, dict):

        if "stocks" in data:
            data = data["stocks"]

        elif "data" in data:
            data = data["data"]

    if not isinstance(data, list):
        raise ValueError(
            "filtered_stocks.json must contain a list of stocks."
        )

    candidates = []

    for record in data:

        if not isinstance(record, dict):
            continue

        security = record.get("security") or {}

        symbol = (
            record.get("symbol")
            or record.get("Symbol")
            or security.get("symbol")
            or ""
        )

        security_id = (
            record.get("securityId")
            or record.get("security_id")
            or record.get("nepse_id")
            or security.get("id")
        )

        if not symbol or not security_id:
            continue

        try:
            security_id = int(security_id)
        except (TypeError, ValueError):
            continue

        candidates.append({
            "symbol": str(symbol).upper(),
            "security_id": security_id,
        })

    # Remove duplicate symbols.
    unique = {}

    for stock in candidates:
        unique[stock["symbol"]] = stock

    candidates = list(unique.values())

    print(
        f"Filtered candidates loaded: {len(candidates)}",
        flush=True
    )

    return candidates


# ============================================================
# STOCK RECORD HELPERS
# ============================================================

def get_symbol(record):

    if not isinstance(record, dict):
        return ""

    security = record.get("security") or {}

    symbol = (
        record.get("symbol")
        or record.get("Symbol")
        or security.get("symbol")
        or ""
    )

    return str(symbol).upper().strip()


def get_security_id(record):

    if not isinstance(record, dict):
        return None

    security = record.get("security") or {}

    return (
        record.get("securityId")
        or record.get("security_id")
        or security.get("id")
        or record.get("nepse_id")
    )


# ============================================================
# PRICE EXTRACTION
# FIXED: NEPSE CURRENT PRICE IS lastUpdatedPrice
# ============================================================

def get_price(record):

    if not isinstance(record, dict):
        return None

    possible_keys = [
        "lastUpdatedPrice",
        "last_updated_price",
        "lastTradedPrice",
        "last_traded_price",
        "ltp",
        "LTP",
        "closePrice",
        "close_price",
        "closingPrice",
        "price",
        "lastPrice",
        "last_price",
        "currentPrice",
        "current_price",
    ]

    for key in possible_keys:

        value = record.get(key)

        if value is None or value == "":
            continue

        try:
            price = float(value)

            if price > 0:
                return price

        except (TypeError, ValueError):
            continue

    # Check nested records if present.
    for nested_key in [
        "security",
        "marketData",
        "market_data",
        "priceData",
        "price_data,
    ]:

        nested = record.get(nested_key)

        if isinstance(nested, dict):

            nested_price = get_price(nested)

            if nested_price is not None:
                return nested_price

    return None


# ============================================================
# HISTORICAL DAILY EMA
# ============================================================

def get_previous_ema(client, security_id):

    try:

        history = client.security_price_history(
            security_id=int(security_id),
            page=1,
            size=HISTORY_SIZE,
        )

        if not isinstance(history, dict):
            return None

        records = history.get("content", [])

        if not records:
            return None

        df = pd.DataFrame(records)

        if "businessDate" not in df.columns:
            print(
                f"Missing businessDate for security {security_id}",
                flush=True
            )
            return None

        if "closePrice" not in df.columns:
            print(
                f"Missing closePrice for security {security_id}",
                flush=True
            )
            return None

        df["Date"] = pd.to_datetime(
            df["businessDate"],
            errors="coerce"
        )

        df["Close"] = pd.to_numeric(
            df["closePrice"],
            errors="coerce"
        )

        df = df.dropna(subset=["Date", "Close"])

        if df.empty:
            return None

        # Sort oldest to newest.
        df = df.sort_values("Date")

        # Exclude today's candle if API has already returned it.
        today = now_nepal().date()

        df = df[
            df["Date"].dt.date < today
        ]

        if len(df) < EMA_LONG:
            return None

        df["EMA7"] = df["Close"].ewm(
            span=EMA_SHORT,
            adjust=False
        ).mean()

        df["EMA21"] = df["Close"].ewm(
            span=EMA_LONG,
            adjust=False
        ).mean()

        latest = df.iloc[-1]

        return {
            "ema7": float(latest["EMA7"]),
            "ema21": float(latest["EMA21"]),
            "previous_close": float(latest["Close"]),
            "date": latest["Date"].strftime("%Y-%m-%d"),
        }

    except Exception as error:

        print(
            f"Historical error for security {security_id}: {error}",
            flush=True
        )

        return None


# ============================================================
# BUILD HISTORICAL EMA CACHE
# ============================================================

def build_ema_cache(client, candidates):

    print(
        "Preparing previous completed daily EMA values...",
        flush=True
    )

    cache = {}

    total = len(candidates)

    for index, stock in enumerate(candidates, start=1):

        symbol = stock["symbol"]
        security_id = stock["security_id"]

        previous = get_previous_ema(
            client,
            security_id
        )

        if previous:
            cache[symbol] = previous

        if index % 25 == 0 or index == total:

            print(
                f"Historical EMA progress: {index}/{total}",
                flush=True
            )

        # Small delay to avoid making requests too quickly.
        time.sleep(0.15)

    print(
        f"Historical EMA cache ready: {len(cache)} stocks",
        flush=True
    )

    return cache


# ============================================================
# TODAY'S PRICE DATA
# ============================================================

def get_today_market_data(client):

    global PRICE_DEBUG_PRINTED

    print(
        "Fetching today's NEPSE price data...",
        flush=True
    )

    raw = client.today_price_all()

    # --------------------------------------------------------
    # DIAGNOSTIC OUTPUT
    # --------------------------------------------------------

    if not PRICE_DEBUG_PRINTED:

        print(
            "\n========== RAW PRICE DATA ==========",
            flush=True
        )

        print(
            "Response type:",
            type(raw).__name__,
            flush=True
        )

        if isinstance(raw, list):

            print(
                "Response record count:",
                len(raw),
                flush=True
            )

            if len(raw) > 0:

                print(
                    "\nFirst record:",
                    flush=True
                )

                print(
                    raw[0],
                    flush=True
                )

                print(
                    "\nFirst record keys:",
                    flush=True
                )

                if isinstance(raw[0], dict):
                    print(
                        list(raw[0].keys()),
                        flush=True
                    )

            if len(raw) > 1:

                print(
                    "\nSecond record:",
                    flush=True
                )

                print(
                    raw[1],
                    flush=True
                )

        elif isinstance(raw, dict):

            print(
                "Response keys:",
                list(raw.keys()),
                flush=True
            )

            print(
                "Response content:",
                raw,
                flush=True
            )

        print(
            "==================================\n",
            flush=True
        )

        PRICE_DEBUG_PRINTED = True

    # --------------------------------------------------------
    # HANDLE RESPONSE
    # --------------------------------------------------------

    if isinstance(raw, dict):

        if "content" in raw:
            raw = raw["content"]

        elif "data" in raw:
            raw = raw["data"]

        else:
            raw = list(raw.values())

    if not isinstance(raw, list):

        print(
            "Unexpected price response format.",
            flush=True
        )

        return {}

    by_symbol = {}

    matched_symbols = []

    for row in raw:

        if not isinstance(row, dict):
            continue

        symbol = get_symbol(row)

        if not symbol:
            continue

        matched_symbols.append(symbol)

        price = get_price(row)

        security_id = get_security_id(row)

        by_symbol[symbol] = {
            "security_id": security_id,
            "price": price,
            "raw_record": row,
        }

    print(
        f"Symbols matched from price response: "
        f"{len(matched_symbols)}",
        flush=True
    )

    print(
        "Sample symbols:",
        matched_symbols[:10],
        flush=True
    )

    readable_prices = sum(
        1
        for row in by_symbol.values()
        if row.get("price") is not None
    )

    print(
        f"Prices successfully extracted: {readable_prices}",
        flush=True
    )

    return by_symbol


# ============================================================
# PROVISIONAL EMA USING TODAY'S PRICE
# ============================================================

def calculate_live_ema(previous_ema, current_price, span):

    alpha = 2 / (span + 1)

    return (
        alpha * current_price
        + (1 - alpha) * previous_ema
    )


# ============================================================
# TELEGRAM ALERT
# ============================================================

def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:

        print(
            "Telegram credentials are missing.",
            flush=True
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if result.get("ok"):

            print(
                "Telegram alert sent successfully.",
                flush=True
            )

            return True

        print(
            f"Telegram returned an error: {result}",
            flush=True
        )

        return False

    except Exception as error:

        print(
            f"Telegram error: {error}",
            flush=True
        )

        return False


# ============================================================
# ALERT MESSAGE
# ============================================================

def create_alert_message(
    symbol,
    signal,
    price,
    ema7,
    ema21,
):

    message = (
        f"📈 NEPSE EMA {EMA_SHORT}/{EMA_LONG} SIGNAL\n\n"
        f"Stock: {symbol}\n"
        f"Signal: {signal}\n"
        f"Price: {price:.2f}\n"
        f"EMA {EMA_SHORT}: {ema7:.2f}\n"
        f"EMA {EMA_LONG}: {ema21:.2f}\n\n"
        f"Time: {print_time()} NPT\n\n"
        f"⚠️ Intraday provisional signal. "
        f"May change before market close."
    )

    return message


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    print("=" * 58, flush=True)

    print(
        " NEPSE CLOUD DAILY EMA 7/21 CROSSOVER MONITOR",
        flush=True
    )

    print(
        " Historical daily candles + today's latest traded price",
        flush=True
    )

    print(
        " Runs weekdays, 11:00 AM to 3:00 PM Nepal time",
        flush=True
    )

    print("=" * 58, flush=True)

    candidates = load_candidates()

    if not candidates:

        print(
            "No filtered stocks found. Scanner stopped.",
            flush=True
        )

        return

    # In-memory alert tracking.
    # One alert per symbol per signal per day.
    sent_alerts = set()

    with NepseClient() as client:

        # Build previous completed daily EMA values.
        ema_cache = build_ema_cache(
            client,
            candidates
        )

        if not ema_cache:

            print(
                "Historical EMA cache is empty. Scanner stopped.",
                flush=True
            )

            return

        # ----------------------------------------------------
        # MONITORING LOOP
        # ----------------------------------------------------

        while True:

            current = now_nepal()

            if not is_weekday():

                print(
                    f"Weekend detected ({print_time()}). "
                    f"Scanner stopped.",
                    flush=True
                )

                break

            if current.time() < MARKET_START:

                print(
                    f"Before market hours ({print_time()}). "
                    f"Waiting until 11:00 AM Nepal time.",
                    flush=True
                )

                time.sleep(60)

                continue

            if current.time() > MARKET_END:

                print(
                    f"Outside market hours ({print_time()}). "
                    f"Monitor stopped.",
                    flush=True
                )

                break

            # ------------------------------------------------
            # FETCH TODAY'S PRICE DATA
            # ------------------------------------------------

            try:

                market_data = get_today_market_data(client)

            except Exception as error:

                print(
                    f"Today's price API error: {error}",
                    flush=True
                )

                print(
                    "Retrying in 5 minutes.",
                    flush=True
                )

                time.sleep(SCAN_INTERVAL)

                continue

            scan_started = time.time()

            print(
                f"Scan started: {print_time()} NPT",
                flush=True
            )

            prices_found = 0
            missing = 0
            crossover_conditions = 0
            alerts_sent = 0

            # ------------------------------------------------
            # SCAN EACH STOCK
            # ------------------------------------------------

            for stock in candidates:

                symbol = stock["symbol"]

                previous = ema_cache.get(symbol)

                market_row = market_data.get(symbol, {})

                price = market_row.get("price")

                if not previous or price is None:

                    missing += 1

                    continue

                try:
                    price = float(price)
                except (TypeError, ValueError):

                    missing += 1

                    continue

                if price <= 0:

                    missing += 1

                    continue

                prices_found += 1

                # Calculate provisional current EMA values.
                live_ema7 = calculate_live_ema(
                    previous["ema7"],
                    price,
                    EMA_SHORT
                )

                live_ema21 = calculate_live_ema(
                    previous["ema21"],
                    price,
                    EMA_LONG
                )

                previous_ema7 = previous["ema7"]
                previous_ema21 = previous["ema21"]

                signal = None

                # BUY crossover.
                if (
                    previous_ema7 <= previous_ema21
                    and live_ema7 > live_ema21
                ):

                    signal = "BUY"

                # SELL crossover.
                elif (
                    previous_ema7 >= previous_ema21
                    and live_ema7 < live_ema21
                ):

                    signal = "SELL"

                if not signal:
                    continue

                crossover_conditions += 1

                # Avoid repeatedly sending the same signal.
                alert_key = (
                    now_nepal().date(),
                    symbol,
                    signal,
                )

                if alert_key in sent_alerts:
                    continue

                message = create_alert_message(
                    symbol=symbol,
                    signal=signal,
                    price=price,
                    ema7=live_ema7,
                    ema21=live_ema21,
                )

                print(
                    f"CROSSOVER: {symbol} | "
                    f"{signal} | Price: {price:.2f}",
                    flush=True
                )

                if send_telegram(message):

                    sent_alerts.add(alert_key)

                    alerts_sent += 1

            # ------------------------------------------------
            # SCAN SUMMARY
            # ------------------------------------------------

            elapsed = time.time() - scan_started

            print(
                f"Scan complete | "
                f"prices: {prices_found} | "
                f"missing: {missing} | "
                f"crossover conditions: {crossover_conditions} | "
                f"alerts sent: {alerts_sent} | "
                f"elapsed: {elapsed:.1f}s",
                flush=True
            )

            # ------------------------------------------------
            # WAIT FOR NEXT SCAN
            # ------------------------------------------------

            print(
                f"Next scan in about "
                f"{max(1, SCAN_INTERVAL - int(elapsed))} seconds.",
                flush=True
            )

            time.sleep(
                max(1, SCAN_INTERVAL - int(elapsed))
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nScanner stopped by user.",
            flush=True
        )

    except Exception as error:

        print(
            f"\nFATAL SCANNER ERROR: {error}",
            flush=True
        )

        raise
