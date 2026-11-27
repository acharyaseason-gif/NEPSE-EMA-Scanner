import json
import os
import subprocess
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from nepsepy import NepseClient

# ============================================================
# NEPSE RSI ALERT MONITOR - separate Telegram bot
# ============================================================
TZ = ZoneInfo("Asia/Kathmandu")
MARKET_START = dt_time(11, 0)
MARKET_END = dt_time(15, 0)
SCAN_INTERVAL_SECONDS = 300
REQUEST_DELAY_SECONDS = 0.35
RSI_PERIOD = 14
RSI_CROSS_LEVEL = 65.0
RSI_OVERBOUGHT_LEVEL = 70.0

ROOT = Path(__file__).resolve().parent
STOCK_FILE = ROOT / "filtered_stocks.json"
STATE_FILE = ROOT / "rsi_alert_state.json"

BOT_TOKEN = os.getenv("RSI_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("RSI_TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")

PRICE_KEYS = (
    "lastUpdatedPrice", "lastTradedPrice", "last_traded_price",
    "ltp", "closePrice", "closingPrice", "close",
)
CLOSE_KEYS = ("closePrice", "closingPrice", "close", "lastUpdatedPrice", "ltp")


def now_local():
    return datetime.now(TZ)


def number_from_record(record, keys):
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            value = float(str(value).replace(",", ""))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return None


def get_symbol(record):
    security = record.get("security") or {}
    return str(record.get("symbol") or security.get("symbol") or "").strip().upper()


def get_security_id(record):
    security = record.get("security") or {}
    return (
        record.get("securityId")
        or record.get("security_id")
        or security.get("id")
        or record.get("nepse_id")
    )


def load_stocks():
    with STOCK_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError("filtered_stocks.json must contain a JSON list")
    stocks = [
        item for item in data
        if isinstance(item, dict) and item.get("symbol")
    ]
    print(f"Filtered stocks loaded: {len(stocks)}")
    return stocks


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except (OSError, json.JSONDecodeError):
            print("WARNING: RSI state file unreadable; starting fresh.")
    return {
        "alerted65": [],
        "armed70": {},
        "last_rsi": {},
    }


def save_state_and_push(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("WARNING: GitHub persistence variables unavailable; state saved locally only.")
        return

    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(["git", "add", STATE_FILE.name], check=True, capture_output=True, text=True)

        commit = subprocess.run(
            ["git", "commit", "-m", "Save NEPSE RSI alert state"],
            capture_output=True, text=True,
        )
        output = (commit.stdout + commit.stderr).lower()
        if commit.returncode != 0:
            if "nothing to commit" in output:
                return
            raise RuntimeError(commit.stderr.strip() or commit.stdout.strip())

        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode != 0:
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(["git", "push"], check=True, capture_output=True, text=True)
        print("Persistent RSI alert state saved to GitHub.")
    except Exception as exc:
        print(f"WARNING: Could not persist RSI alert state: {exc}")


def get_today_prices(client):
    raw = client.today_price_all()
    by_symbol = {}
    for row in raw or []:
        symbol = get_symbol(row)
        if not symbol:
            continue
        by_symbol[symbol] = {
            "security_id": get_security_id(row),
            "price": number_from_record(row, PRICE_KEYS),
        }
    return by_symbol


def get_historical_closes(client, security_id, today):
    response = client.security_price_history(
        security_id=int(security_id), page=1, size=100
    )
    records = response.get("content", []) if isinstance(response, dict) else []
    closes_by_date = {}

    for record in records:
        business_date = record.get("businessDate")
        if not business_date:
            continue
        try:
            date_value = datetime.strptime(str(business_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        # Exclude today's partial candle. Current price is appended separately.
        if date_value >= today:
            continue
        close = number_from_record(record, CLOSE_KEYS)
        if close is not None:
            closes_by_date[date_value] = close

    return [close for _, close in sorted(closes_by_date.items())]


def calculate_rsi_wilder(closes, period=14):
    """Return the latest Wilder RSI, or None if there isn't enough price history."""
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing RSI_TELEGRAM_BOT_TOKEN or RSI_TELEGRAM_CHAT_ID secret")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url, json={"chat_id": CHAT_ID, "text": message}, timeout=25
    )
    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Telegram returned a non-JSON response")
    if response.status_code != 200 or not result.get("ok"):
        description = result.get("description", response.text)
        raise RuntimeError(f"Telegram error {response.status_code}: {description}")


def format_alert(alert_type, symbol, name, price, previous_rsi, current_rsi):
    now = now_local()
    if alert_type == "cross65":
        heading = "🟢 NEPSE RSI CROSSED ABOVE 65"
        detail = "RSI(14) moved from 65 or below to above 65."
        status = "📈 Momentum threshold crossed"
    else:
        heading = "🔴 NEPSE RSI 70 RETEST"
        detail = "RSI(14) was above 70 and has now returned to 70 or below."
        status = "🔄 Overbought-level retest"

    lines = [
        heading,
        "━━━━━━━━━━━━━━━━━━",
        f"🏢 {name or symbol} ({symbol})",
        f"💰 Latest price: Rs. {price:,.2f}" if price is not None else "💰 Latest price: unavailable",
        f"📊 Previous RSI(14): {previous_rsi:.2f}",
        f"📊 Current RSI(14): {current_rsi:.2f}",
        status,
        detail,
        f"⏰ {now:%Y-%m-%d %H:%M} NPT",
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ Intraday provisional RSI; not a buy/sell recommendation.",
    ]
    return "\n".join(lines)


def main():
    print("=" * 58)
    print(" NEPSE FILTERED-STOCK RSI ALERT MONITOR")
    print(" RSI(14) | Levels 65 and 70 | 5-minute scan | 11 AM-3 PM NPT")
    print("=" * 58)

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Add RSI_TELEGRAM_BOT_TOKEN and RSI_TELEGRAM_CHAT_ID GitHub secrets")

    stocks = load_stocks()
    stock_by_symbol = {str(stock["symbol"]).upper(): stock for stock in stocks}
    state = load_state()
    alerted65 = set(state.setdefault("alerted65", []))
    armed70 = state.setdefault("armed70", {})
    last_rsi = state.setdefault("last_rsi", {})
    cached_date = None
    historical = {}

    with NepseClient() as client:
        while True:
            now = now_local()
            if now.weekday() >= 5:
                print("Weekend in Nepal. Exiting.")
                return
            if now.time() < MARKET_START:
                opening = datetime.combine(now.date(), MARKET_START, tzinfo=TZ)
                wait_seconds = max(1, (opening - now).total_seconds())
                print(f"Before market open; waiting {wait_seconds / 60:.1f} minutes.")
                time.sleep(wait_seconds)
                continue
            if now.time() >= MARKET_END:
                print("Market session ended. Exiting.")
                return

            today = now.date()
            today_text = today.isoformat()
            if cached_date != today_text:
                print("Loading historical closes for RSI(14)...")
                prices = get_today_prices(client)
                historical = {}
                loaded = 0
                for index, (symbol, stock) in enumerate(stock_by_symbol.items(), start=1):
                    current = prices.get(symbol, {})
                    security_id = current.get("security_id") or get_security_id(stock)
                    if not security_id:
                        print(f"[{index}/{len(stock_by_symbol)}] {symbol}: no security ID")
                        continue
                    try:
                        closes = get_historical_closes(client, security_id, today)
                        if len(closes) >= RSI_PERIOD + 1:
                            historical[symbol] = closes
                            loaded += 1
                        else:
                            print(f"[{index}/{len(stock_by_symbol)}] {symbol}: insufficient close history")
                    except Exception as exc:
                        print(f"[{index}/{len(stock_by_symbol)}] {symbol}: history error: {exc}")
                    time.sleep(REQUEST_DELAY_SECONDS)
                cached_date = today_text
                print(f"Historical RSI data ready: {loaded} stocks")

            scan_started = time.time()
            prices = get_today_prices(client)
            checked = missing = sent = 0
            state_changed = False
            print(f"\nScan started: {now_local():%H:%M:%S} NPT")

            for symbol, stock in stock_by_symbol.items():
                current = prices.get(symbol, {})
                price = current.get("price")
                closes = historical.get(symbol)
                if price is None or not closes:
                    missing += 1
                    continue

                # Previous RSI is based on completed daily candles; current RSI
                # uses the same candles plus the latest available price.
                previous_rsi = calculate_rsi_wilder(closes, RSI_PERIOD)
                current_rsi = calculate_rsi_wilder(closes + [price], RSI_PERIOD)
                if previous_rsi is None or current_rsi is None:
                    missing += 1
                    continue
                checked += 1

                # Arm a 70 retest when RSI has been above 70. This state persists
                # until the RSI returns to 70 or below and the alert is delivered.
                if current_rsi > RSI_OVERBOUGHT_LEVEL:
                    if not armed70.get(symbol, False):
                        armed70[symbol] = True
                        state_changed = True
                elif previous_rsi > RSI_OVERBOUGHT_LEVEL and not armed70.get(symbol, False):
                    armed70[symbol] = True
                    state_changed = True

                daily_key = f"{today_text}:{symbol}:cross65"
                crossing65 = previous_rsi <= RSI_CROSS_LEVEL < current_rsi
                if crossing65 and daily_key not in alerted65:
                    message = format_alert(
                        "cross65", symbol, str(stock.get("name") or symbol),
                        price, previous_rsi, current_rsi,
                    )
                    try:
                        send_telegram(message)
                        alerted65.add(daily_key)
                        state["alerted65"] = sorted(alerted65)
                        save_state_and_push(state)
                        sent += 1
                        print(f"RSI >65 alert sent: {symbol} | RSI {current_rsi:.2f}")
                    except Exception as exc:
                        print(f"RSI 65 alert failed for {symbol}: {exc}")

                if armed70.get(symbol, False) and current_rsi <= RSI_OVERBOUGHT_LEVEL:
                    message = format_alert(
                        "retest70", symbol, str(stock.get("name") or symbol),
                        price, previous_rsi, current_rsi,
                    )
                    try:
                        send_telegram(message)
                        armed70[symbol] = False
                        state_changed = True
                        save_state_and_push(state)
                        sent += 1
                        print(f"RSI 70 retest alert sent: {symbol} | RSI {current_rsi:.2f}")
                    except Exception as exc:
                        print(f"RSI 70 retest alert failed for {symbol}: {exc}")

                last_rsi[symbol] = round(current_rsi, 4)

            if state_changed:
                save_state_and_push(state)

            elapsed = time.time() - scan_started
            print(f"Scan complete | checked={checked} missing={missing} alerts={sent} | {elapsed:.1f}s")
            wait = max(1, SCAN_INTERVAL_SECONDS - elapsed)
            print(f"Next scan in about {wait:.0f} seconds.")
            time.sleep(wait)


if __name__ == "__main__":
    main()
