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
# NEPSE VOLUME SPIKE MONITOR - separate Telegram bot
# ============================================================
TZ = ZoneInfo("Asia/Kathmandu")
MARKET_START = dt_time(11, 0)
MARKET_END = dt_time(15, 0)
SCAN_INTERVAL_SECONDS = 300
REQUEST_DELAY_SECONDS = 0.35
HISTORY_DAYS = 20
DAILY_SPIKE_MULTIPLE = 2.0       # Today's cumulative volume >= 2x average daily volume
BURST_AVG_FRACTION = 0.10        # New volume in one scan interval >= 10% of average daily volume

ROOT = Path(__file__).resolve().parent
STOCK_FILE = ROOT / "filtered_stocks.json"
STATE_FILE = ROOT / "volume_alert_state.json"
BOT_TOKEN = os.getenv("VOLUME_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("VOLUME_TELEGRAM_CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")

VOLUME_KEYS = (
    "totalTradedQuantity", "totalTradeQuantity", "totalTradedVolume",
    "totalVolume", "tradedQuantity", "tradedVolume", "volume", "quantity",
)
PRICE_KEYS = ("lastUpdatedPrice", "lastTradedPrice", "last_traded_price", "ltp", "closePrice")


def now_local():
    return datetime.now(TZ)


def positive_number(record, keys):
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value not in (None, "", 0, "0"):
            try:
                number = float(str(value).replace(",", ""))
                if number >= 0:
                    return number
            except (TypeError, ValueError):
                continue
    return None


def get_symbol(record):
    security = record.get("security") or {}
    return str(record.get("symbol") or security.get("symbol") or "").strip().upper()


def get_security_id(record):
    security = record.get("security") or {}
    return (record.get("securityId") or record.get("security_id") or
            security.get("id") or record.get("nepse_id"))


def load_stocks():
    with STOCK_FILE.open("r", encoding="utf-8") as file:
        stocks = json.load(file)
    if not isinstance(stocks, list):
        raise ValueError("filtered_stocks.json must contain a JSON list")
    stocks = [stock for stock in stocks if isinstance(stock, dict) and stock.get("symbol")]
    print(f"Filtered stocks loaded: {len(stocks)}")
    return stocks


def load_state(today_text):
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    else:
        state = {}
    if state.get("date") != today_text:
        state = {"date": today_text, "alerted": []}
    state.setdefault("alerted", [])
    return state


def save_state_and_push(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    # Persist alert keys in the repository so a restarted runner won't resend them.
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("WARNING: GitHub token/repository unavailable; alert history saved locally only.")
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True, capture_output=True)
        subprocess.run(["git", "add", STATE_FILE.name], check=True)
        commit = subprocess.run(["git", "commit", "-m", "Save NEPSE volume alert history"], capture_output=True, text=True)
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
            raise RuntimeError(commit.stderr.strip() or commit.stdout.strip())
        if commit.returncode == 0:
            # Rebase any unrelated workflow commit before pushing, then retry once.
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode != 0:
                subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True, capture_output=True, text=True)
                subprocess.run(["git", "push"], check=True, capture_output=True, text=True)
        print("Persistent volume alert history saved to GitHub.")
    except Exception as exc:
        print(f"WARNING: Could not persist alert history to GitHub: {exc}")


def get_today_market_data(client):
    raw = client.today_price_all()
    by_symbol = {}
    for row in raw or []:
        symbol = get_symbol(row)
        if not symbol:
            continue
        by_symbol[symbol] = {
            "security_id": get_security_id(row),
            "price": positive_number(row, PRICE_KEYS),
            "volume": positive_number(row, VOLUME_KEYS),
        }
    return by_symbol


def get_average_daily_volume(client, security_id, today):
    history = client.security_price_history(
        security_id=int(security_id), page=1, size=100
    )
    records = history.get("content", []) if isinstance(history, dict) else []
    rows = {}
    for record in records:
        business_date = record.get("businessDate")
        if not business_date:
            continue
        try:
            date_value = datetime.strptime(str(business_date)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_value >= today:
            continue
        volume = positive_number(record, VOLUME_KEYS)
        if volume is not None:
            rows[date_value] = volume
    recent = sorted(rows.items())[-HISTORY_DAYS:]
    if len(recent) < 5:
        return None
    average = sum(volume for _, volume in recent) / len(recent)
    return {"average": average, "sessions": len(recent), "latest_date": recent[-1][0].isoformat()}


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing VOLUME_TELEGRAM_BOT_TOKEN or VOLUME_TELEGRAM_CHAT_ID secret")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=25)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned error: {result}")


def format_alert(symbol, name, price, current_volume, average_volume, ratio, burst_volume, tags):
    now = now_local()
    increase = (ratio - 1) * 100
    lines = [
        "🚨 NEPSE EXCESSIVE VOLUME ALERT",
        "━━━━━━━━━━━━━━━━━━",
        f"🏢 {name or symbol} ({symbol})",
        f"💰 Latest price: Rs. {price:,.2f}" if price is not None else "💰 Latest price: unavailable",
        f"📦 Today's volume: {current_volume:,.0f} shares",
        f"📊 20-session average: {average_volume:,.0f} shares",
        f"🔥 Volume ratio: {ratio:.2f}× average (+{increase:.1f}%)",
    ]
    if burst_volume is not None:
        lines.append(f"⚡ Volume since previous scan: {burst_volume:,.0f} shares")
    lines += [
        f"🔔 Trigger: {', '.join(tags)}",
        f"⏰ {now:%Y-%m-%d %H:%M} NPT",
        "━━━━━━━━━━━━━━━━━━",
        "⚠️ Unusual activity only; not a buy/sell recommendation. Volume is cumulative and signals can change.",
    ]
    return "\n".join(lines)


def main():
    print("=" * 58)
    print(" NEPSE FILTERED-STOCK VOLUME SPIKE MONITOR")
    print(" Separate Telegram bot | 5-minute scan | 11 AM-3 PM NPT")
    print("=" * 58)
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Add VOLUME_TELEGRAM_BOT_TOKEN and VOLUME_TELEGRAM_CHAT_ID GitHub secrets")

    stocks = load_stocks()
    stock_by_symbol = {str(stock["symbol"]).upper(): stock for stock in stocks}
    averages = {}
    last_volume = {}
    cached_date = None

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
            state = load_state(today_text)
            alerted = set(state["alerted"])

            if cached_date != today_text:
                print("Loading 20-session average volume for filtered stocks...")
                market_data = get_today_market_data(client)
                averages = {}
                for index, (symbol, stock) in enumerate(stock_by_symbol.items(), start=1):
                    row = market_data.get(symbol, {})
                    security_id = row.get("security_id") or get_security_id(stock)
                    if not security_id:
                        print(f"[{index}/{len(stock_by_symbol)}] {symbol}: no security ID")
                        continue
                    try:
                        avg = get_average_daily_volume(client, security_id, today)
                        if avg and avg["average"] > 0:
                            averages[symbol] = avg
                        else:
                            print(f"[{index}/{len(stock_by_symbol)}] {symbol}: insufficient volume history")
                    except Exception as exc:
                        print(f"[{index}/{len(stock_by_symbol)}] {symbol}: history error: {exc}")
                    time.sleep(REQUEST_DELAY_SECONDS)
                cached_date = today_text
                print(f"Average-volume baseline ready: {len(averages)} stocks")

            scan_started = time.time()
            market_data = get_today_market_data(client)
            checked = missing = sent = 0
            print(f"\nScan started: {now_local():%H:%M:%S} NPT")
            for symbol, stock in stock_by_symbol.items():
                current = market_data.get(symbol, {})
                volume = current.get("volume")
                price = current.get("price")
                avg_data = averages.get(symbol)
                if volume is None or not avg_data:
                    missing += 1
                    continue
                checked += 1
                average = avg_data["average"]
                ratio = volume / average if average else 0
                previous_volume = last_volume.get(symbol)
                delta = max(0.0, volume - previous_volume) if previous_volume is not None else None
                last_volume[symbol] = volume

                tags = []
                trigger_keys = []
                daily_key = f"{today_text}:{symbol}:daily-spike"
                burst_key = f"{today_text}:{symbol}:volume-burst"
                if ratio >= DAILY_SPIKE_MULTIPLE and daily_key not in alerted:
                    tags.append(f"Daily volume ≥ {DAILY_SPIKE_MULTIPLE:.1f}× average")
                    trigger_keys.append(daily_key)
                if (delta is not None and delta >= average * BURST_AVG_FRACTION
                        and burst_key not in alerted):
                    tags.append(f"5-minute volume burst ≥ {BURST_AVG_FRACTION:.0%} of average daily volume")
                    trigger_keys.append(burst_key)
                if not tags:
                    continue

                name = str(stock.get("name") or symbol)
                message = format_alert(symbol, name, price, volume, average, ratio, delta, tags)
                try:
                    send_telegram(message)
                    alerted.update(trigger_keys)
                    state["alerted"] = sorted(alerted)
                    save_state_and_push(state)
                    sent += 1
                    print(f"Volume alert sent: {symbol} | {', '.join(tags)}")
                except Exception as exc:
                    print(f"Alert failed for {symbol}: {exc}")

            elapsed = time.time() - scan_started
            print(f"Scan complete | checked={checked} missing={missing} alerts={sent} | {elapsed:.1f}s")
            wait = max(1, SCAN_INTERVAL_SECONDS - elapsed)
            print(f"Next scan in about {wait:.0f} seconds.")
            time.sleep(wait)


if __name__ == "__main__":
    main()
