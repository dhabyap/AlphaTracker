import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import json
from typing import Optional

from app.services.dexscreener import DexScreenerService
from app.services.analyzer import Analyzer
from app.services.ai_advisor import AIAdvisor
from app.services.notifier import send_telegram, check_alerts
from app.database.models import Database

app = FastAPI(title="Binance Alpha Tracker")

# Init
db = Database()
dex = DexScreenerService()
analyzer = Analyzer()
ai = AIAdvisor()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
async def health():
    return {"status": "ok", "tokens": db.count_tokens()}


@app.get("/api/tokens")
async def list_tokens(
    sort: str = Query("score", description="Sort by: score, volume, mc, age"),
    limit: int = Query(50, le=200),
):
    """List all tracked tokens with latest analysis."""
    tokens = db.get_all_tokens(sort=sort, limit=limit)
    return {"tokens": tokens, "total": len(tokens)}


@app.get("/api/token/{contract}")
async def get_token(contract: str):
    """Get single token detail + live data from DexScreener."""
    # Check DB first
    token = db.get_token(contract)
    if token:
        # Refresh from DexScreener
        pairs = dex.search(contract)
        if pairs:
            analysis = analyzer.analyze(pairs)
            db.save_analysis(contract, analysis)
            return {"token": token, "analysis": analysis, "pairs": pairs}
    return JSONResponse({"error": "Token not found"}, status_code=404)


@app.post("/api/track/{contract}")
async def track_token(contract: str, symbol: str = "", name: str = ""):
    """Add a token to tracking list."""
    pairs = dex.search(contract)
    if not pairs:
        raise HTTPException(400, "Contract not found on DexScreener")

    bt = pairs[0].get("baseToken", {})
    symbol = symbol or bt.get("symbol", "?")
    name = name or bt.get("name", "")

    token_id = db.add_token(contract, symbol, name, pairs[0].get("chainId", "bsc"))

    analysis = analyzer.analyze(pairs)
    db.save_analysis(contract, analysis)

    return {"status": "tracked", "token_id": token_id, "analysis": analysis}


@app.get("/api/signals")
async def get_signals(limit: int = Query(20, le=100)):
    """Get tokens with active signals."""
    tokens = db.get_all_tokens(limit=limit)
    result = []
    for t in tokens:
        results = json.loads(t.get("latest_analysis", "{}")) if t.get("latest_analysis") else {}
        signals = results.get("signals", [])
        if signals:
            result.append({"contract": t["contract"], "symbol": t["symbol"], "signals": signals, "score": results.get("score", 0)})
    return {"signals": sorted(result, key=lambda x: -x["score"])}


@app.get("/api/history/{contract}")
async def get_history(contract: str, limit: int = Query(20, le=100)):
    """Get analysis history for a token (for chart)."""
    rows = db.get_history(contract, limit=limit)
    return {"history": rows}


@app.get("/api/scan")
async def scan_token(contract: str = Query(...), chain: str = Query("bsc")):
    """Scan a single token — get full analysis."""
    pairs = dex.search(contract)
    if not pairs:
        raise HTTPException(404, "Token not found")

    analysis = analyzer.analyze(pairs)
    return {"contract": contract, "analysis": analysis, "pairs": pairs}


@app.get("/api/batch-scan")
async def batch_scan(contracts: str = Query(...)):
    """Scan multiple contracts. Comma-separated."""
    clist = [c.strip() for c in contracts.split(",") if c.strip()]
    results = []
    for c in clist[:10]:  # Max 10
        pairs = dex.search(c)
        if pairs:
            analysis = analyzer.analyze(pairs)
            results.append({"contract": c, "analysis": analysis})
    return {"results": results}


@app.get("/api/ai-advisor")
async def ai_advisor(contract: str = Query(...), force: int = Query(0, description="1=force refresh AI")):
    """AI-powered analysis & recommendation for a token. Cached in DB for 1 hour."""
    pairs = dex.search(contract)
    if not pairs:
        raise HTTPException(404, "Token not found")

    analysis = analyzer.analyze(pairs)

    # Check DB cache first (unless force refresh)
    if not force:
        cached = db.get_ai_recommendation(contract, max_age_seconds=3600)
        if cached:
            return {
                "contract": contract,
                "analysis": analysis,
                "ai": cached,
                "_from_cache": True,
            }

    # No cache or expired — hit AI
    m = analysis["metrics"]
    signals_str = "; ".join(f"{s['label']} ({s.get('detail','')})" for s in analysis.get("signals", []))

    context = (
        f"Token: ${m.get('symbol','?')}\n"
        f"Harga: ${m.get('price_usd','?')}\n"
        f"Perubahan 24h: {m.get('price_change_24h','?')}%\n"
        f"Likuiditas: ${m.get('liquidity_usd',0):,.0f}\n"
        f"Volume 24h: ${m.get('volume_24h',0):,.0f}\n"
        f"Market Cap: ${m.get('market_cap',0):,.0f}\n"
        f"Rasio Beli/Jual: {m.get('buy_sell_ratio','?')}x\n"
        f"Umur: {m.get('age_days','?')} hari\n"
        f"Supply Float: {m.get('float_pct','?')}%\n"
        f"Trading Pairs: {m.get('markets',0)}\n"
        f"Chain: {pairs[0].get('chainId','?')}\n"
        f"Sinyal: {signals_str}\n"
        f"Skor: {analysis['score']}/100\n"
        f"Kontrak: {contract[:10]}...{contract[-6:]}"
    )

    # Inject manual corrections if any
    token = db.get_token(contract)
    if token and token.get("notes"):
        try:
            notes = json.loads(token["notes"]) if isinstance(token["notes"], str) else token["notes"]
            if notes:
                corrections = "\n".join(f"KOREKSI: {k}={v}" for k, v in notes.items())
                context += f"\n\nKOREKSI DATA (override data DexScreener dengan ini):\n{corrections}"
        except:
            pass

    try:
        recommendation = ai.analyze(context)
        # Save to DB cache
        db.save_ai_recommendation(contract, recommendation)
    except Exception as e:
        # Return cached even if expired, or fallback
        cached = db.get_ai_recommendation(contract, max_age_seconds=999999)
        if cached:
            return {
                "contract": contract,
                "analysis": analysis,
                "ai": cached,
                "_from_cache": True,
                "_warning": f"AI error: {e}. Using old cache.",
            }
        recommendation = {
            "verdict": "error",
            "confidence": 0,
            "risk_level": "unknown",
            "reasoning": f"Gagal menghubungi AI: {e}",
            "key_factors": [],
            "hold_until": "",
        }

    return {
        "contract": contract,
        "analysis": analysis,
        "ai": recommendation,
    }


@app.post("/api/refresh-all")
async def refresh_all():
    """Refresh all tracked tokens and send alerts on changes."""
    old_tokens = db.get_all_tokens(limit=100)
    old_snapshots = []
    for t in old_tokens:
        a = t.get("latest_analysis") or {}
        if isinstance(a, str):
            a = json.loads(a) if a else {}
        m = a.get("metrics", {})
        old_snapshots.append({
            "contract": t["contract"],
            "symbol": t["symbol"],
            "score": a.get("score", 0),
            "price": m.get("price_usd", 0),
        })

    refreshed = 0
    errors = 0
    for t in old_tokens:
        try:
            pairs = dex.search(t["contract"])
            if pairs:
                analysis = analyzer.analyze(pairs)
                db.save_analysis(t["contract"], analysis)
                refreshed += 1
        except:
            errors += 1

    # Check for alerts
    new_tokens = db.get_all_tokens(limit=100)
    new_snapshots = []
    for t in new_tokens:
        a = t.get("latest_analysis") or {}
        if isinstance(a, str):
            a = json.loads(a) if a else {}
        m = a.get("metrics", {})
        new_snapshots.append({
            "contract": t["contract"],
            "symbol": t["symbol"],
            "score": a.get("score", 0),
            "price": m.get("price_usd", 0),
        })

    alerts = check_alerts(old_snapshots, new_snapshots)

    # Send Telegram summary
    summary = f"🔍 *AlphaTracker Refresh*\n{refreshed} tokens refreshed"
    if errors:
        summary += f"\n⚠️ {errors} errors"
    summary += f"\n{'📬 ' + str(len(alerts)) + ' alerts' if alerts else '✅ No significant changes'}"
    send_telegram(summary)

    if alerts:
        for alert in alerts[:5]:  # Max 5 alerts per refresh
            send_telegram(alert)

    return {
        "status": "ok",
        "refreshed": refreshed,
        "errors": errors,
        "alerts": len(alerts),
    }


@app.delete("/api/tokens/{contract}")
async def delete_token(contract: str):
    """Remove a tracked token."""
    token = db.get_token(contract)
    if not token:
        raise HTTPException(404, "Token not found")
    db.remove_token(contract)
    return {"status": "removed", "contract": contract}


@app.patch("/api/correct/{contract}")
async def correct_token(contract: str, corrections: dict):
    """Store manual corrections for a token (age, float, listing, etc)."""
    token = db.get_token(contract)
    if not token:
        raise HTTPException(404, "Token not found")
    
    existing = {}
    if token.get("notes"):
        try: existing = json.loads(token["notes"]) if isinstance(token["notes"], str) else token["notes"]
        except: pass
    
    existing.update(corrections)
    db.set_notes(contract, existing)
    return {"status": "updated", "corrections": existing}


@app.patch("/api/portfolio/{contract}")
async def set_portfolio(contract: str, body: dict = Body(...)):
    """Set portfolio entry: buy_price, buy_amount, buy_date."""
    token = db.get_token(contract)
    if not token:
        raise HTTPException(404, "Token not found")
    buy_price = float(body.get("buy_price", 0))
    buy_amount = float(body.get("buy_amount", 0))
    buy_date = body.get("buy_date", "")
    if buy_price <= 0 or buy_amount <= 0:
        raise HTTPException(400, "buy_price and buy_amount must be > 0")
    db.set_portfolio(contract, buy_price, buy_amount, buy_date)
    return {"status": "saved", "buy_price": buy_price, "buy_amount": buy_amount, "buy_date": buy_date}


@app.delete("/api/portfolio/{contract}")
async def clear_portfolio(contract: str):
    """Remove portfolio entry for a token."""
    token = db.get_token(contract)
    if not token:
        raise HTTPException(404, "Token not found")
    db.clear_portfolio(contract)
    return {"status": "cleared"}


@app.get("/api/portfolio")
async def get_portfolio():
    """Get portfolio summary with P&L."""
    return db.get_portfolio_summary()


@app.get("/api/sparkline/{contract}")
async def get_sparkline(contract: str, limit: int = Query(20, le=100)):
    """Get price history for sparkline chart."""
    rows = db.get_history(contract, limit=limit)
    points = []
    for h in reversed(rows):
        a = h.get("analysis", {})
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except:
                continue
        price = a.get("metrics", {}).get("price_usd", 0)
        if price:
            points.append(price)
    return {"contract": contract, "prices": points}


@app.get("/api/alerts")
async def get_alerts():
    """Get recent alert history."""
    from app.services.notifier import get_alert_history
    return {"alerts": get_alert_history()}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8003, reload=True)
