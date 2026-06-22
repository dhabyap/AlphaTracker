import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query
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
async def ai_advisor(contract: str = Query(...)):
    """AI-powered analysis & recommendation for a token."""
    pairs = dex.search(contract)
    if not pairs:
        raise HTTPException(404, "Token not found")

    analysis = analyzer.analyze(pairs)
    m = analysis["metrics"]
    signals_str = "; ".join(f"{s['label']} ({s.get('detail','')})" for s in analysis.get("signals", []))
    
    context = (
        f"Token: ${m.get('symbol','?')}\n"
        f"Price: ${m.get('price_usd','?')}\n"
        f"24h Change: {m.get('price_change_24h','?')}%\n"
        f"Liquidity: ${m.get('liquidity_usd',0):,.0f}\n"
        f"24h Volume: ${m.get('volume_24h',0):,.0f}\n"
        f"Market Cap: ${m.get('market_cap',0):,.0f}\n"
        f"Buy/Sell Ratio: {m.get('buy_sell_ratio','?')}x\n"
        f"Age: {m.get('age_days','?')} days\n"
        f"Supply Float: {m.get('float_pct','?')}%\n"
        f"Trading Pairs: {m.get('markets',0)}\n"
        f"Chain: {pairs[0].get('chainId','?')}\n"
        f"Signals: {signals_str}\n"
        f"Score: {analysis['score']}/100\n"
        f"Contract: {contract[:10]}...{contract[-6:]}"
    )

    # Inject manual corrections if any
    token = db.get_token(contract)
    if token and token.get("notes"):
        try:
            notes = json.loads(token["notes"]) if isinstance(token["notes"], str) else token["notes"]
            if notes:
                corrections = "\n".join(f"CORRECTIONS: {k}={v}" for k, v in notes.items())
                context += f"\n\nIMPORTANT CORRECTIONS (override DexScreener data with these):\n{corrections}"
        except:
            pass

    recommendation = ai.analyze(context)
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


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8003, reload=True)
