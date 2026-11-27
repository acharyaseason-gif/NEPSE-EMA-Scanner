import os
import json
import time
import subprocess
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
ALERT_HISTORY_FILE = BASE_DIR / "alert_history.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

PRICE_DEBUG_PRINTED = False


# ============================================================
# TIME HELPERS
# ============================================================

def now_nepal():
    return datetime.now(TZ)


def print_time():
    return now_nepal().strftime("%Y-%m-%d %H:%M:%S")


def is_weekday():
    return now_nepal().weekday() < 5


# ============================================================
# LOAD FILTERED STOCKS
# ============================================================

def load_candidates():
    if not STOCK_FILE.exists():
        raise FileNotFoundError(f"Stock file not found: {STOCK_FILE}")

    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = data.get("stocks", data.get("data", data))

    if not isinstance(data, list):
        raise ValueError("filtered_stocks.json must contain a list of stocks")

    unique = {}

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

        if not symbol or security_id is None:
            continue

        try:
            security_id = int(security_id)
        except (TypeError, ValueError):
            continue

        symbol = str(symbol).upper().strip()
        company_name = (
            record.get("name")
            or record.get("companyName")
            or record.get("company_name")
            or record.get("securityName")
            or security.get("name")
            or symbol
        )

        unique[symbol] = {
            "symbol": symbol,
            "security_id": security_id,
            "name": str(company_name).strip(),
        }

    candidates = list(unique.values())
    print(f"Filtered candidates loaded: {len(candidates)}", flush=True)
    return candidates


# ============================================================
# API RECORD HELPERS
# ============================================================

def get_symbol(record):
    if not isinstance(record, dict):
        return ""

    security = record.get("security") or {}
    symbol = record.get("symbol") or record.get("Symbol") or security.get("symbol") or ""
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


def get_price(record):
    """NEPSE's current intraday price is returned in lastUpdatedPrice."""
    if not isinstance(record, dict):
        return None

    price_keys = (
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
    )

    for key in price_keys:
        value = record.get(key)
        if value is None or value == "":
            continue

        try:
            price = float(value)
            if price > 0:
                return price
        except (TypeError, ValueError):
            pass

    # Support nested market-price objects, if the API returns them.
    for nested_key in (
        "security",
        "marketData",
        "market_data",
        "priceData",
        "price_data",
    ):
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

        if "businessDate" not in df.columns or "closePrice" not in df.columns:
            return None

        df["Date"] = pd.to_datetime(df["businessDate"], errors="coerce")
        df["Close"] = pd.to_numeric(df["closePrice"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"])

        if df.empty:
            return None

        df = df.sort_values("Date")

        # Use only completed daily candles before today.
        today = now_nepal().date()
        df = df[df["Date"].dt.date < today]

        if len(df) < EMA_LONG:
            return None

        df["EMA7"] = df["Close"].ewm(span=EMA_SHORT, adjust=False).mean()
        df["EMA21"] = df["Close"].ewm(span=EMA_LONG, adjust=False).mean()

        latest = df.iloc[-1]

        return {
            "ema7": float(latest["EMA7"]),
            "ema21": float(latest["EMA21"]),
            "previous_close": float(latest["Close"]),
            "date": latest["Date"].strftime("%Y-%m-%d"),
        }

    except Exception as exc:
        print(f"Historical error for security {security_id}: {exc}", flush=True)
        return None


def build_ema_cache(client, candidates):
    print("Preparing previous completed daily EMA values...", flush=True)

    cache = {}
    total = len(candidates)

    for index, stock in enumerate(candidates, start=1):
        result = get_previous_ema(client, stock["security_id"])

        if result:
            cache[stock["symbol"]] = result

        if index % 25 == 0 or index == total:
            print(f"Historical EMA progress: {index}/{total}", flush=True)

        time.sleep(0.15)

    print(f"Historical EMA cache ready: {len(cache)} stocks", flush=True)
    return cache


# ============================================================
# TODAY'S PRICE DATA
# ============================================================

def get_today_market_data(client):
    global PRICE_DEBUG_PRINTED

    print("Fetching today's NEPSE price data...", flush=True)
    raw = client.today_price_all()

    if not PRICE_DEBUG_PRINTED:
        print("\\n========== RAW PRICE DATA ==========", flush=True)
        print(f"Response type: {type(raw).__name__}", flush=True)

        if isinstance(raw, list):
            print(f"Response record count: {len(raw)}", flush=True)
            if raw:
                print("First record:", flush=True)
                print(raw[0], flush=True)
                if isinstance(raw[0], dict):
                    print("First record keys:", list(raw[0].keys()), flush=True)
            if len(raw) > 1:
                print("Second record:", flush=True)
                print(raw[1], flush=True)

        elif isinstance(raw, dict):
            print("Response keys:", list(raw.keys()), flush=True)
            print("Response content:", raw, flush=True)

        print("==================================\\n", flush=True)
        PRICE_DEBUG_PRINTED = True

    if isinstance(raw, dict):
        if isinstance(raw.get("content"), list):
            raw = raw["content"]
        elif isinstance(raw.get("data"), list):
            raw = raw["data"]
        else:
            print("Unexpected dictionary price response structure.", flush=True)
            return {}

    if not isinstance(raw, list):
        print("Unexpected price response format.", flush=True)
        return {}

    by_symbol = {}
    symbols = []

    for row in raw:
        if not isinstance(row, dict):
            continue

        symbol = get_symbol(row)
        if not symbol:
            continue

        price = get_price(row)
        by_symbol[symbol] = {
            "security_id": get_security_id(row),
            "price": price,
        }
        symbols.append(symbol)

    prices_read = sum(
        1 for record in by_symbol.values()
        if record.get("price") is not None
    )

    print(f"Symbols matched from price response: {len(symbols)}", flush=True)
    print(f"Sample symbols: {symbols[:10]}", flush=True)
    print(f"Prices successfully extracted: {prices_read}", flush=True)

    return by_symbol


# ============================================================
# PERSISTENT ALERT HISTORY
# ============================================================

def load_alert_history():
    """Load alert keys from disk so repeated scans/restarts do not resend."""
    if not ALERT_HISTORY_FILE.exists():
        return set()

    try:
        with open(ALERT_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            keys = data.get("sent_alerts", [])
        elif isinstance(data, list):
            # Also accept a simple list for compatibility.
            keys = data
        else:
            keys = []

        today = now_nepal().date()
        retained = set()
        for key in keys:
            key = str(key)
            date_part = key.split("|", 1)[0]
            try:
                key_date = datetime.strptime(date_part, "%Y-%m-%d").date()
            except ValueError:
                continue
            if 0 <= (today - key_date).days <= 30:
                retained.add(key)
        return retained
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read alert history ({exc}); starting with empty history.", flush=True)
        return set()


def save_alert_history(sent_alerts):
    """Write alert history atomically to avoid a partially-written JSON file."""
    temporary_file = ALERT_HISTORY_FILE.with_suffix(".json.tmp")
    payload = {
        "updated_at_npt": print_time(),
        "sent_alerts": sorted(sent_alerts),
    }

    with open(temporary_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    temporary_file.replace(ALERT_HISTORY_FILE)


def persist_alert_history_to_github():
    """Commit history to the repo so a fresh GitHub runner can restore it.

    Requires workflow permission: contents: write and checkout credentials.
    If GitHub persistence fails, local history is still saved for this run.
    """
    if not os.getenv("GITHUB_ACTIONS"):
        return

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            cwd=BASE_DIR, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=BASE_DIR, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "add", ALERT_HISTORY_FILE.name],
            cwd=BASE_DIR, check=True, capture_output=True, text=True,
        )

        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=BASE_DIR, capture_output=True,
        )
        if status.returncode == 0:
            return

        subprocess.run(
            ["git", "commit", "-m", "Update Telegram alert history"],
            cwd=BASE_DIR, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=BASE_DIR, check=True, capture_output=True, text=True,
        )
        print("Alert history saved to GitHub for future workflow runs.", flush=True)

    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"WARNING: Could not persist alert history to GitHub: {details}", flush=True)
        print("Local alert history remains saved for this workflow run.", flush=True)


# ============================================================
# EMA AND TELEGRAM
# ============================================================

def calculate_live_ema(previous_ema, current_price, span):
    alpha = 2.0 / (span + 1.0)
    return alpha * current_price + (1.0 - alpha) * previous_ema


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials are missing from GitHub Secrets.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()

        if result.get("ok"):
            print("Telegram alert sent successfully.", flush=True)
            return True

        print(f"Telegram returned an error: {result}", flush=True)
        return False

    except Exception as exc:
        print(f"Telegram error: {exc}", flush=True)
        return False


def create_alert_message(symbol, signal, price, ema7, ema21, company_name=None):
    signal_emoji = "🟢" if signal == "BUY" else "🔴"
    signal_title = "BULLISH EMA CROSSOVER" if signal == "BUY" else "BEARISH EMA CROSSOVER"
    gap = ema7 - ema21
    gap_percent = (gap / ema21 * 100.0) if ema21 else 0.0
    display_name = company_name or symbol

    return (
        f"{signal_emoji} NEPSE EMA {EMA_SHORT}/{EMA_LONG} {signal} SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏢 Stock: {display_name} ({symbol})\n"
        f"📊 Signal: {signal_title}\n\n"
        f"💰 Latest Price: Rs. {price:,.2f}\n"
        f"📉 EMA {EMA_SHORT}: Rs. {ema7:,.2f}\n"
        f"📈 EMA {EMA_LONG}: Rs. {ema21:,.2f}\n"
        f"↔️ EMA Gap: Rs. {gap:,.2f} ({gap_percent:+.2f}%)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Time: {now_nepal().strftime('%I:%M %p')} NPT\n"
        f"📅 Date: {now_nepal().strftime('%d %b %Y')}\n\n"
        "⚠️ Provisional intraday signal; it may change before market close."
    )


# ============================================================
# MAIN SCANNER
# ============================================================

def main():
    print("=" * 58, flush=True)
    print(" NEPSE CLOUD DAILY EMA 7/21 CROSSOVER MONITOR", flush=True)
    print(" Historical daily candles + today's latest traded price", flush=True)
    print(" Runs weekdays, 11:00 AM to 3:00 PM Nepal time", flush=True)
    print("=" * 58, flush=True)

    candidates = load_candidates()
    if not candidates:
        print("No filtered stocks found. Scanner stopped.", flush=True)
        return

    sent_alerts = load_alert_history()
    print(f"Previously sent alert keys loaded: {len(sent_alerts)}", flush=True)

    with NepseClient() as client:
        ema_cache = build_ema_cache(client, candidates)

        if not ema_cache:
            print("Historical EMA cache is empty. Scanner stopped.", flush=True)
            return

        while True:
            current = now_nepal()

            if not is_weekday():
                print(f"Weekend detected ({print_time()} NPT). Scanner stopped.", flush=True)
                break

            if current.time() < MARKET_START:
                print(f"Before market hours ({print_time()} NPT). Waiting until 11:00 AM.", flush=True)
                time.sleep(60)
                continue

            if current.time() > MARKET_END:
                print(f"Outside market hours ({print_time()} NPT). Monitor stopped.", flush=True)
                break

            try:
                market_data = get_today_market_data(client)
            except Exception as exc:
                print(f"Today's price API error: {exc}", flush=True)
                print("Retrying in 5 minutes.", flush=True)
                time.sleep(SCAN_INTERVAL)
                continue

            scan_started = time.time()
            print(f"Scan started: {print_time()} NPT", flush=True)

            prices_found = 0
            missing = 0
            crossover_conditions = 0
            alerts_sent = 0

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

                live_ema7 = calculate_live_ema(previous["ema7"], price, EMA_SHORT)
                live_ema21 = calculate_live_ema(previous["ema21"], price, EMA_LONG)

                old_ema7 = previous["ema7"]
                old_ema21 = previous["ema21"]

                signal = None

                if old_ema7 <= old_ema21 and live_ema7 > live_ema21:
                    signal = "BUY"
                elif old_ema7 >= old_ema21 and live_ema7 < live_ema21:
                    signal = "SELL"

                if not signal:
                    continue

                crossover_conditions += 1

                # One alert per stock, direction, and Nepal trading date.
                # A new opposite signal can still alert on the same day.
                alert_key = f"{now_nepal().date().isoformat()}|{symbol}|{signal}"
                if alert_key in sent_alerts:
                    continue

                company_name = stock.get("name") or stock.get("companyName")
                message = create_alert_message(
                    symbol=symbol,
                    signal=signal,
                    price=price,
                    ema7=live_ema7,
                    ema21=live_ema21,
                    company_name=company_name,
                )

                print(
                    f"CROSSOVER: {symbol} | {signal} | Price: {price:.2f}",
                    flush=True,
                )

                if send_telegram(message):
                    sent_alerts.add(alert_key)
                    save_alert_history(sent_alerts)
                    persist_alert_history_to_github()
                    alerts_sent += 1

            elapsed = time.time() - scan_started

            print(
                f"Scan complete | prices: {prices_found} | missing: {missing} | "
                f"crossover conditions: {crossover_conditions} | "
                f"alerts sent: {alerts_sent} | elapsed: {elapsed:.1f}s",
                flush=True,
            )

            wait_seconds = max(1, SCAN_INTERVAL - int(elapsed))
            print(f"Next scan in about {wait_seconds} seconds.", flush=True)
            time.sleep(wait_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\\nScanner stopped by user.", flush=True)
    except Exception as exc:
        print(f"\\nFATAL SCANNER ERROR: {exc}", flush=True)
        raise
