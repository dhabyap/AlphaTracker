"""Telegram bot for Binance Alpha Tracker — query tokens, get signals."""

import sys
import os
import json
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.dexscreener import DexScreenerService
from app.services.analyzer import Analyzer
from app.database.models import Database

TELEGRAM_TOKEN = os.environ.get("ALPHA_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
BACKEND_URL = os.environ.get("ALPHA_BACKEND", "http://localhost:8003")

dex = DexScreenerService()
analyzer = Analyzer()
db = Database()


def send_msg(chat_id: str, text: str, parse_mode: str = "HTML"):
    """Send Telegram message."""
    if not TELEGRAM_TOKEN:
        print(f"[BOT] Would send to {chat_id}: {text[:100]}...")
        return
    try:
        requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=5,
        )
    except Exception as e:
        print(f"[BOT] Send error: {e}")


def format_token_analysis(contract: str, data: dict) -> str:
    """Format token analysis as HTML message."""
    a = data.get("analysis", {})
    m = a.get("metrics", {})
    signals = a.get("signals", [])

    lines = [
        f"🔍 <b>Alpha Scan</b>",
        f"<code>{contract[:10]}...{contract[-6:]}</code>",
        f"",
        f"💰 <b>{m.get('symbol', '?')}</b> ${m.get('price_usd', '?')}",
    ]

    if m.get("price_change_24h") is not None:
        ch = m["price_change_24h"]
        icon = "🟢" if ch >= 0 else "🔴"
        lines.append(f"{icon} 24h: {ch:+.2f}%")

    lines.extend([
        f"💧 Liq: ${m.get('liquidity_usd', 0):,.0f}",
        f"📊 Vol 24h: ${m.get('volume_24h', 0):,.0f}",
    ])

    if m.get("market_cap"):
        lines.append(f"🏦 MC: ${m['market_cap']:,.0f}")

    if m.get("buy_sell_ratio") is not None:
        lines.append(f"🔄 Buy/Sell: {m['buy_sell_ratio']}x")
    if m.get("float_pct") is not None:
        lines.append(f"📦 Float: {m['float_pct']}%")
    if m.get("age_days") is not None:
        lines.append(f"📅 {m['age_days']} days old")

    lines.append(f"")

    if signals:
        for s in signals:
            icons = {
                "bullish": "🟢", "bearish": "🔴", "warning": "⚠️",
                "safe": "✅", "hot": "🔥", "info": "ℹ️"
            }
            lines.append(f"{icons.get(s['type'], '•')} {s['label']}")
            if s.get("detail"):
                lines[-1] += f" — {s['detail']}"

    lines.append(f"")

    # Score bar
    score = a.get("score", 0)
    bar = "🟩" * max(0, score // 10) + "⬜" * max(0, 10 - score // 10)
    lines.append(f"<b>Score: {score}/100</b>")
    lines.append(bar)
    lines.append(f"<b>{a.get('rec_label', '?')}</b>")
    lines.append(f"")

    if m.get("url"):
        lines.append(f"🔗 <a href='{m['url']}'>DexScreener</a>")
    lines.append(f"📊 <a href='http://localhost:8003'>AlphaTracker</a>")

    return "\n".join(lines)


def format_token_list(tokens: list) -> str:
    """Format token list as HTML."""
    if not tokens:
        return "No tokens tracked yet."

    lines = ["📋 <b>Alpha Tracker — Tokens</b>", ""]
    for t in tokens[:15]:
        a = t.get("latest_analysis", "{}")
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except:
                a = {}
        score = a.get("score", 0) if a else 0
        rec = a.get("rec_label", "?") if a else "?"
        contract = t.get("contract", "")[:8]
        lines.append(
            f"<b>{t.get('symbol', '?')}</b> [{score}] {rec}"
            f"\n<code>{contract}...</code>"
        )

    lines.append(f"\nTotal: {len(tokens)} tokens")
    return "\n".join(lines)


def handle_message(chat_id: str, text: str):
    """Process incoming message."""
    text = text.strip()

    if text.startswith("/alpha ") or text.startswith("/alpha@"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_msg(chat_id, "Usage: /alpha {contract_address}")
            return
        query = parts[1].strip()

        if query == "list":
            tokens = db.get_all_tokens(limit=20)
            send_msg(chat_id, format_token_list(tokens))
            return

        if query.startswith("track "):
            addr = query[6:].strip()
            pairs = dex.search(addr)
            if not pairs:
                send_msg(chat_id, "❌ Contract not found on DexScreener")
                return
            bt = pairs[0].get("baseToken", {})
            db.add_token(addr, bt.get("symbol", "?"), bt.get("name", ""))
            analysis = analyzer.analyze(pairs)
            db.save_analysis(addr, analysis)
            send_msg(chat_id, f"✅ Tracked {bt.get('symbol', '?')}\n\n" + format_token_analysis(addr, {"analysis": analysis}))
            return

        # Default: scan address
        pairs = dex.search(query)
        if not pairs:
            send_msg(chat_id, "❌ Token not found. Try a valid contract address.")
            return
        analysis = analyzer.analyze(pairs)
        send_msg(chat_id, format_token_analysis(query, {"analysis": analysis}))
        return

    if text == "/start":
        send_msg(chat_id, "🤖 <b>Alpha Tracker Bot</b>\n\n"
                 "Commands:\n"
                 "/alpha {address} — Analyze token\n"
                 "/alpha list — Tracked tokens\n"
                 "/alpha track {address} — Add to tracking\n"
                 f"\nWeb: http://localhost:8003")
        return


if __name__ == "__main__":
    import time

    if not TELEGRAM_TOKEN:
        print("❌ ALPHA_BOT_TOKEN not set. Set env var or edit script.")
        print("Usage: ALPHA_BOT_TOKEN=xxx python bot/telegram.py")
        sys.exit(1)

    print("🤖 Alpha Tracker Bot starting...")
    last_update_id = 0

    while True:
        try:
            r = requests.get(
                f"{API_URL}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35,
            )
            if r.status_code != 200:
                time.sleep(5)
                continue

            data = r.json()
            for update in data.get("result", []):
                update_id = update.get("update_id", 0)
                if update_id > last_update_id:
                    last_update_id = update_id

                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id", "")
                text = msg.get("text", "")

                if text:
                    handle_message(str(chat_id), text)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"[BOT] Error: {e}")
            time.sleep(5)
