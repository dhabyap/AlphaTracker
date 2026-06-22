"""Telegram notifier + alert engine for AlphaTracker."""

import os
import json
import time
import requests

TOKEN = os.environ.get("ALPHA_BOT_TOKEN", "")
CHAT_ID = os.environ.get("ALPHA_CHAT_ID", "1042928926")
API_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

# In-memory alert history (last 20 alerts)
_alert_history = []
MAX_ALERTS = 20

def add_alert_to_history(message: str, alert_type: str = "info"):
    """Add alert to in-memory history."""
    _alert_history.append({
        "message": message,
        "type": alert_type,
        "timestamp": int(time.time()),
    })
    # Keep only last MAX_ALERTS
    if len(_alert_history) > MAX_ALERTS:
        _alert_history.pop(0)

def get_alert_history() -> list:
    """Get recent alerts."""
    return list(reversed(_alert_history))

def send_telegram(message: str) -> bool:
    """Send message to Telegram. Returns True if sent."""
    if not API_URL:
        print(f"[notifier] No bot token. Would send: {message[:100]}...")
        return False
    try:
        r = requests.post(f"{API_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        ok = r.status_code == 200
        if not ok:
            print(f"[notifier] Telegram send failed: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[notifier] Telegram error: {e}")
        return False


def format_token_change(token, old_score=None, new_score=None, old_price=None, new_price=None) -> str:
    """Format alert message for a token change."""
    sym = token.get("symbol", "?")
    emoji = "🟢" if (new_score or 0) >= (old_score or 0) else "🔴"
    msg = f"{emoji} *${sym}*"
    if old_score is not None and new_score is not None:
        diff = new_score - old_score
        arrow = "▲" if diff > 0 else "▼"
        msg += f" Score: {old_score} → {new_score} ({arrow}{abs(diff)})"
    if old_price is not None and new_price is not None:
        pct = ((new_price - old_price) / old_price) * 100 if old_price else 0
        arrow_p = "📈" if pct > 0 else "📉"
        msg += f" Price: ${new_price:.4f} ({arrow_p}{pct:+.1f}%)"
    msg += f"\n🔗 {token.get('contract', '')[:10]}..."
    return msg


def check_alerts(before: list, after: list, threshold_score_drop: int = 10, threshold_pump: float = 20.0) -> list:
    """Compare tokens before/after scan. Returns list of alert messages."""
    alerts = []
    before_map = {t["contract"]: t for t in before}
    after_map = {t["contract"]: t for t in after}

    # New tokens appeared
    for t in after:
        if t["contract"] not in before_map:
            sym = t.get("symbol", "?")
            score = t.get("score", 0)
            price = t.get("price", 0)
            msg = f"🆕 *New Token Tracked: ${sym}*\nScore: {score}/100 | Price: ${price:.4f}"
            alerts.append(msg)
            add_alert_to_history(msg, "new_token")

    # Existing tokens changed
    for contract, t in after_map.items():
        if contract not in before_map:
            continue
        b = before_map[contract]
        old_score = b.get("score", 0) or 0
        new_score = t.get("score", 0) or 0
        old_price = b.get("price", 0) or 0
        new_price = t.get("price", 0) or 0

        # Score drop
        if old_score - new_score >= threshold_score_drop:
            msg = format_token_change(t, old_score, new_score, old_price, new_price) + "\n⚠️ Significant score drop!"
            alerts.append(msg)
            add_alert_to_history(msg, "score_drop")

        # Price pump
        if old_price and new_price:
            pct = ((new_price - old_price) / old_price) * 100
            if pct >= threshold_pump:
                msg = format_token_change(t, old_score, new_score, old_price, new_price) + f"\n🔥 Pump {pct:.0f}%!"
                alerts.append(msg)
                add_alert_to_history(msg, "pump")
            elif pct <= -threshold_pump:
                msg = format_token_change(t, old_score, new_score, old_price, new_price) + f"\n💀 Dump {abs(pct):.0f}%!"
                alerts.append(msg)
                add_alert_to_history(msg, "dump")

    return alerts
