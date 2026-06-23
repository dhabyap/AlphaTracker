import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import json
import time
import threading
from typing import Optional

from app.services.dexscreener import DexScreenerService
from app.services.analyzer import Analyzer
from app.services.ai_advisor import AIAdvisor
from app.services.notifier import send_telegram, check_alerts
from app.services.whale_tracker import WhaleTracker
from app.services.whale_discovery import WhaleDiscovery
from app.database.models import Database

app = FastAPI(title="Binance Alpha Tracker")

# Init
db = Database()
dex = DexScreenerService()
analyzer = Analyzer()
ai = AIAdvisor()
whale_tracker = WhaleTracker(db)
whale_discovery = WhaleDiscovery(db)

# ── Background whale crawl thread ──────────────────────────────────────────────
_whale_crawl_lock = threading.Lock()

def _bg_crawl_loop():
    while True:
        try:
            result = whale_tracker.crawl_all()
            if result.get("crawled", 0) > 0 or result.get("total_trades", 0) > 0:
                from app.services.notifier import add_alert_to_history
                add_alert_to_history(
                    f"🐋 Auto crawl: {result['crawled']} wallets, {result['total_trades']} new trades",
                    "whale")
        except Exception as e:
            print(f"[BG Crawl] Error: {e}")
        time.sleep(1800)  # 30 min

t = threading.Thread(target=_bg_crawl_loop, daemon=True)
t.start()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/token/{contract}")
async def token_detail_page(contract: str):
    token = db.get_token(contract)
    if not token:
        return HTMLResponse("<h1>Token not found</h1>", status_code=404)
    html_path = os.path.join(os.path.dirname(__file__), "templates", "token_detail.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>Template not found</h1>", status_code=404)
    with open(html_path, encoding="utf-8") as f:
        content = f.read()
    content = content.replace("{{CONTRACT}}", contract)
    content = content.replace("{{SYMBOL}}", (token.get("symbol") or "?").replace('"', "&quot;"))
    return HTMLResponse(content)


@app.get("/whale/{wallet}")
async def whale_detail_page(wallet: str):
    whale_data = db.get_whale(wallet)
    if not whale_data:
        return HTMLResponse("<h1>Whale not found</h1>", status_code=404)
    html_path = os.path.join(os.path.dirname(__file__), "templates", "whale_detail.html")
    with open(html_path, encoding="utf-8") as f:
        content = f.read()
    label = whale_data.get("label", "Whale")
    content = content.replace("{{WALLET}}", wallet).replace("{{LABEL}}", label.replace('"', "&quot;"))
    return HTMLResponse(content)


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


@app.patch("/api/token/{contract}/notes")
async def update_token_notes(contract: str, payload: dict):
    """Manually set fundamental data (holders, supply, etc) to enrich AI analysis."""
    notes = payload.get("notes", {})
    token = db.get_token(contract)
    if not token:
        raise HTTPException(404, "Token not tracked")
    db.update_token_notes(contract, json.dumps(notes))
    return {"status": "updated", "notes": notes}


@app.get("/api/token/{contract}/whale-trades")
async def token_whale_trades(contract: str, limit: int = Query(50, le=200)):
    """Get whale trades involving this token."""
    trades = db.get_trades_by_token(contract, limit=limit)
    return {"trades": trades}


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
        f"Nama: {m.get('name','?')}\n"
        f"Harga: ${m.get('price_usd','?')}\n"
        f"Chain: {pairs[0].get('chainId','?')}\n"
        f"Perubahan 24h: {m.get('price_change_24h','?')}%\n"
        f"Jumlah Pair: {m.get('markets',0)}\n"
        f"\n── LIKUIDITAS & MODAL ──\n"
        f"Likuiditas: ${m.get('liquidity_usd',0):,.0f}\n"
        f"Market Cap: ${m.get('market_cap',0):,.0f}\n"
        f"FDV: ${m.get('fdv',0):,.0f}\n"
        f"MC/FDV: {m.get('mc_fdv_ratio','?')}x (1.0 = fully diluted)\n"
        f"MC/Liq: {m.get('mc_liq_ratio','?')}x (semakin kecil = lebih aman)\n"
        f"\n── VOLUME & AKTIVITAS ──\n"
        f"Volume 24h: ${m.get('volume_24h',0):,.0f}\n"
        f"Vol/MC: {m.get('vol_mc_ratio','?')}% (aktivitas relatif)\n"
        f"Vol/Liq: {m.get('vol_liq_ratio','?')}x (volume surge)\n"
        f"Buy: {m.get('txns_buy',0):,} | Sell: {m.get('txns_sell',0):,}\n"
        f"Buy/Sell: {m.get('buy_sell_ratio','?')}x\n"
        f"\n── SUPPLY ──\n"
        f"Supply Float: {m.get('float_pct','?')}%\n"
        f"\n── UMUR PROYEK ──\n"
        f"Umur: {m.get('age_days','?')} hari\n"
        f"\n── SISTEM ──\n"
        f"Sinyal: {signals_str}\n"
        f"Skor Teknikal: {analysis['score']}/100\n"
        f"Kontrak: {contract[:10]}...{contract[-6:]}"
    )

    # Inject manual corrections if any
    token = db.get_token(contract)
    if token and token.get("notes"):
        try:
            notes = json.loads(token["notes"]) if isinstance(token["notes"], str) else token["notes"]
            if notes:
                corrections = "\n".join(f"KOREKSI: {k}={v}" for k, v in notes.items())
                context += f"\n\nKOREKSI DATA (manual dari user, override DexScreener):\n{corrections}"
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


# ── WHALE DISCOVERY API ─────────────────────────────────────────────────────────
@app.get("/api/whales/discover")
async def discover_whales(limit: int = Query(10, le=20)):
    """Scan BSC for new large wallets not yet tracked."""
    candidates = whale_discovery.discover(limit=limit)
    return {"candidates": candidates, "count": len(candidates)}


# ── WHALE API ───────────────────────────────────────────────────────────────────
@app.get("/api/whales")
async def list_whales():
    """Return list of tracked whale wallets."""
    return {"whales": db.get_whales()}

@app.post("/api/whales")
async def add_whale_endpoint(payload: dict):
    """Add a new whale to track."""
    wallet = payload.get("wallet", "").strip()
    label = payload.get("label", "")
    tx_hashes = payload.get("tx_hashes", [])
    if not wallet:
        raise HTTPException(400, "wallet address required")
    existing = db.get_whale(wallet)
    if existing:
        if tx_hashes:
            imported = whale_discovery.import_trades(wallet, tx_hashes)
            return {"status": "exists", "wallet": wallet, "trades_imported": imported}
        return {"status": "exists", "wallet": wallet}
    db.add_whale(wallet, label)
    result = {"status": "added", "wallet": wallet, "label": label}
    return result

@app.delete("/api/whales/{wallet}")
async def remove_whale(wallet: str):
    """Remove a tracked whale."""
    db.delete_whale(wallet)
    return {"status": "deleted", "wallet": wallet}

@app.get("/api/whales/{wallet}/trades")
async def get_whale_trades(wallet: str, limit: int = Query(50, le=200)):
    """Get trade history for a specific whale wallet."""
    trades = db.get_whale_trades(wallet, limit=limit)
    return {"wallet": wallet, "trades": trades, "count": len(trades)}

@app.post("/api/whales/crawl")
async def trigger_whale_crawl():
    """Manually trigger whale crawl."""
    if _whale_crawl_lock.acquire(blocking=False):
        try:
            def _do_crawl():
                try:
                    result = whale_tracker.crawl_all()
                    from app.services.notifier import add_alert_to_history
                    if result.get("crawled", 0) > 0 or result.get("total_trades", 0) > 0:
                        add_alert_to_history(
                            f"🐋 Manual crawl: {result['crawled']} wallets, {result['total_trades']} trades",
                            "whale")
                finally:
                    _whale_crawl_lock.release()
            t = threading.Thread(target=_do_crawl, daemon=True)
            t.start()
            return {"status": "started", "message": "Crawl started in background"}
        finally:
            pass
    return {"status": "busy", "message": "Crawl already in progress"}

@app.post("/api/whales/{wallet}/crawl")
async def trigger_single_whale_crawl(wallet: str):
    """Crawl a single whale wallet immediately."""
    result = whale_tracker.crawl_wallet(wallet)
    return {"status": "ok", "wallet": wallet, "result": result}

@app.post("/api/whales/{wallet}/fill-prices")
async def fill_whale_prices(wallet: str):
    """Fill missing price data for whale trades via DexScreener."""
    result = whale_tracker.fill_prices(wallet)
    return {"status": "ok", "wallet": wallet, "result": result}

@app.get("/api/whales/{wallet}/ai-analysis")
async def whale_ai_analysis(wallet: str):
    """AI analysis of whale trading behavior."""
    stats = db.update_whale_stats(wallet)
    trades = db.get_whale_trades(wallet, limit=20)
    whale_data = db.get_whale(wallet)
    label = whale_data.get("label", "Whale") if whale_data else "Whale"
    if not trades:
        return {"status": "no_data", "analysis": None}
    lines = []
    for t in trades[:10]:
        aksi = "Beli" if t["trade_type"] == "buy" else "Jual" if t["trade_type"] == "sell" else t["trade_type"]
        sym = t.get("token_symbol", "?")
        amt = t.get("amount", 0) or 0
        val = t.get("value_usd", 0) or 0
        when = t.get("trade_at", 0)
        lines.append(f"- {aksi} ${sym}: {amt:.4f} token, ${val:.2f}, time={when}")
    trades_summary = "\n".join(lines)
    try:
        result = ai.analyze_whale(label, wallet, trades_summary, stats)
        return {"status": "ok", "wallet": wallet, "analysis": result, "stats": stats}
    except Exception as e:
        return {"status": "error", "message": str(e), "analysis": None}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8003, reload=True)
