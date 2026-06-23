"""Analysis engine — evaluate token and generate signals."""

import time
from datetime import datetime, timezone


class Analyzer:
    def analyze(self, pairs: list) -> dict:
        """Full analysis from DexScreener pairs data."""
        if not pairs:
            return {"score": 0, "signals": [], "metrics": {}}

        best = pairs[0]  # Primary pair

        # Extract data
        p = self._extract(best, pairs)

        signals = []
        score = 50  # Base score

        # 1. Low Float detection
        if p["circ_supply"] and p["total_supply"] and p["total_supply"] > 0:
            float_pct = (p["circ_supply"] / p["total_supply"]) * 100
            p["float_pct"] = round(float_pct, 1)
            if float_pct < 10:
                signals.append({"type": "warning", "label": "⚠️ Low Float", "detail": f"{float_pct:.1f}% circulating", "severity": "high"})
                score -= 15
            elif float_pct < 30:
                signals.append({"type": "info", "label": "📊 Moderate Float", "detail": f"{float_pct:.1f}% circulating"})
                score += 3
            elif float_pct > 60:
                signals.append({"type": "safe", "label": "✅ High Float", "detail": f"{float_pct:.1f}% circulating"})
                score += 5
            else:
                signals.append({"type": "info", "label": "📊 Moderate Float", "detail": f"{float_pct:.1f}% circulating"})
        else:
            p["float_pct"] = None

        # 2. MC/FDV ratio — dilution risk
        if p["market_cap"] and p["fdv"] and p["fdv"] > 0:
            mc_fdv_ratio = p["market_cap"] / p["fdv"]
            p["mc_fdv_ratio"] = round(mc_fdv_ratio, 4)
            if mc_fdv_ratio > 0.8:
                signals.append({"type": "safe", "label": "✅ Fully Diluted", "detail": f"MC={mc_fdv_ratio:.0%} of FDV"})
                score += 10
            elif mc_fdv_ratio > 0.5:
                signals.append({"type": "info", "label": "📊 Low Dilution", "detail": f"MC={mc_fdv_ratio:.0%} of FDV"})
                score += 5
            elif mc_fdv_ratio > 0.2:
                signals.append({"type": "warning", "label": "⚠️ Dilution Risk", "detail": f"MC only {mc_fdv_ratio:.0%} of FDV", "severity": "medium"})
                score -= 5
            else:
                signals.append({"type": "bearish", "label": "🔴 High Dilution Risk", "detail": f"MC only {mc_fdv_ratio:.0%} of FDV", "severity": "high"})
                score -= 15
        else:
            p["mc_fdv_ratio"] = None

        # 3. Buy/Sell ratio
        total_txns = p["txns_buy"] + p["txns_sell"]
        if total_txns > 10:
            ratio = p["txns_buy"] / max(p["txns_sell"], 1)
            p["buy_sell_ratio"] = round(ratio, 2)
            if ratio > 1.5:
                signals.append({"type": "bullish", "label": "🟢 Accumulation", "detail": f"Buy/Sell: {ratio:.2f}", "severity": "high"})
                score += 20
            elif ratio > 1.0:
                signals.append({"type": "info", "label": "📈 Slight Buying", "detail": f"Buy/Sell: {ratio:.2f}"})
                score += 5
            elif ratio < 0.5:
                signals.append({"type": "bearish", "label": "🔴 Dumping", "detail": f"Buy/Sell: {ratio:.2f}", "severity": "high"})
                score -= 15
        else:
            p["buy_sell_ratio"] = None

        # 4. Volume / MC ratio — activity level
        if p["market_cap"] and p["market_cap"] > 0 and p["volume_24h"] > 0:
            vol_mc_ratio = p["volume_24h"] / p["market_cap"]
            p["vol_mc_ratio"] = round(vol_mc_ratio, 4)
            if vol_mc_ratio > 0.5:
                signals.append({"type": "bullish", "label": "🔥 High Activity", "detail": f"Vol/MC: {vol_mc_ratio:.2%}", "severity": "high"})
                score += 15
            elif vol_mc_ratio > 0.1:
                signals.append({"type": "info", "label": "📊 Active Trading", "detail": f"Vol/MC: {vol_mc_ratio:.2%}"})
                score += 5
            elif vol_mc_ratio < 0.01:
                signals.append({"type": "warning", "label": "💤 Low Activity", "detail": f"Vol/MC: {vol_mc_ratio:.2%}", "severity": "low"})
                score -= 5
        else:
            p["vol_mc_ratio"] = None

        # 5. Liquidity depth — MC/Liq ratio (lower = safer)
        if p["liquidity_usd"] and p["market_cap"] and p["liquidity_usd"] > 0 and p["market_cap"] > 0:
            mc_liq_ratio = p["market_cap"] / p["liquidity_usd"]
            p["mc_liq_ratio"] = round(mc_liq_ratio, 2)
            if mc_liq_ratio < 10:
                signals.append({"type": "safe", "label": "✅ Deep Liquidity", "detail": f"MC/Liq: {mc_liq_ratio:.0f}x", "severity": "low"})
                score += 10
            elif mc_liq_ratio < 50:
                signals.append({"type": "info", "label": "💧 Moderate Depth", "detail": f"MC/Liq: {mc_liq_ratio:.0f}x"})
                score += 3
            elif mc_liq_ratio < 200:
                signals.append({"type": "warning", "label": "⚠️ Thin Liquidity", "detail": f"MC/Liq: {mc_liq_ratio:.0f}x", "severity": "medium"})
                score -= 5
            else:
                signals.append({"type": "bearish", "label": "🔴 Very Thin Liquidity", "detail": f"MC/Liq: {mc_liq_ratio:.0f}x", "severity": "high"})
                score -= 15
        else:
            p["mc_liq_ratio"] = None

        # 6. Liquidity check (absolute)
        if p["liquidity_usd"]:
            if p["liquidity_usd"] > 500000:
                signals.append({"type": "safe", "label": "✅ High Liquidity", "detail": f"${p['liquidity_usd']:,.0f}"})
                score += 10
            elif p["liquidity_usd"] > 50000:
                signals.append({"type": "info", "label": "💧 Medium Liquidity", "detail": f"${p['liquidity_usd']:,.0f}"})
                score += 5
            elif p["liquidity_usd"] < 10000:
                signals.append({"type": "warning", "label": "⚠️ Low Liquidity", "detail": f"${p['liquidity_usd']:,.0f}", "severity": "high"})
                score -= 15
            else:
                signals.append({"type": "info", "label": "💧 Adequate Liquidity", "detail": f"${p['liquidity_usd']:,.0f}"})

        # 7. Age check
        if p["pair_created_at"]:
            age_days = (time.time() - p["pair_created_at"] / 1000) / 86400
            p["age_days"] = round(age_days, 1)
            if age_days < 1:
                signals.append({"type": "hot", "label": "🆕 Just Launched!", "detail": f"< 1 day ago"})
                score += 5
            elif age_days < 7:
                signals.append({"type": "hot", "label": "🆕 New Token", "detail": f"{age_days:.0f} days old"})
                score += 3
            elif age_days > 90:
                signals.append({"type": "safe", "label": "📅 Mature", "detail": f"{age_days:.0f} days old"})
                score += 5
        else:
            p["age_days"] = None

        # 8. Price change check
        if p["price_change_24h"] is not None:
            if p["price_change_24h"] > 50:
                signals.append({"type": "warning", "label": "🚀 Mooning", "detail": f"+{p['price_change_24h']:.1f}% in 24h", "severity": "high"})
                score += 5
            elif p["price_change_24h"] > 10:
                signals.append({"type": "bullish", "label": "📈 Pumping", "detail": f"+{p['price_change_24h']:.1f}% in 24h"})
                score += 5
            elif p["price_change_24h"] < -30:
                signals.append({"type": "bearish", "label": "📉 Heavy Dump", "detail": f"{p['price_change_24h']:.1f}% in 24h", "severity": "high"})
                score -= 10
            elif p["price_change_24h"] > 0:
                signals.append({"type": "info", "label": "📗 Green", "detail": f"+{p['price_change_24h']:.1f}%"})

        # 9. Number of pairs (markets) — more = better distribution
        p["markets"] = len(pairs)
        if len(pairs) >= 5:
            signals.append({"type": "safe", "label": "🏛️ Multi Markets", "detail": f"{len(pairs)} pairs"})
            score += 8
        elif len(pairs) >= 3:
            signals.append({"type": "safe", "label": "🏛️ Multiple Markets", "detail": f"{len(pairs)} pairs"})
            score += 5
        elif len(pairs) >= 2:
            signals.append({"type": "info", "label": "🏛️ 2 Markets", "detail": f"{len(pairs)} pair"})
            score += 2

        # 10. Volume surge detection (huge vol = whale attention)
        if p["volume_24h"] and p["liquidity_usd"] and p["liquidity_usd"] > 0:
            vol_liq_ratio = p["volume_24h"] / p["liquidity_usd"]
            p["vol_liq_ratio"] = round(vol_liq_ratio, 2)
            if vol_liq_ratio > 10:
                signals.append({"type": "hot", "label": "🔥 Volume Surge", "detail": f"Vol/Liq: {vol_liq_ratio:.0f}x", "severity": "high"})
                score += 10

        # Clamp score
        score = max(0, min(100, score))

        # Hold recommendation
        if score >= 70:
            recommendation = "strong_buy"
            rec_label = "✅ Strong Buy & Hold"
        elif score >= 50:
            recommendation = "buy"
            rec_label = "📌 Buy & Watch"
        elif score >= 30:
            recommendation = "hold"
            rec_label = "👀 Monitor Only"
        else:
            recommendation = "avoid"
            rec_label = "⛔ Avoid / Too Risky"

        return {
            "score": score,
            "recommendation": recommendation,
            "rec_label": rec_label,
            "signals": signals,
            "metrics": p,
        }

    def _extract(self, best: dict, all_pairs: list) -> dict:
        """Extract flat metrics from pairs data."""
        bt = best.get("baseToken", {})
        liq_all = sum((p.get("liquidity", {}).get("usd", 0) or 0) for p in all_pairs)
        vol_all = sum((p.get("volume", {}).get("h24", 0) or 0) for p in all_pairs)
        txns_buy = sum((p.get("txns", {}).get("h24", {}).get("buys", 0) or 0) for p in all_pairs)
        txns_sell = sum((p.get("txns", {}).get("h24", {}).get("sells", 0) or 0) for p in all_pairs)

        # Try to get supply info from labels (DexScreener might not have this)
        supply_labels = best.get("labels", [])
        circ_supply = None
        total_supply = None
        for label in supply_labels:
            if "supply" in label.lower():
                parts = label.split(":")
                if len(parts) == 2:
                    try:
                        if "circulating" in label.lower():
                            circ_supply = float(parts[1])
                        elif "total" in label.lower():
                            total_supply = float(parts[1])
                    except ValueError:
                        pass

        return {
            "symbol": bt.get("symbol", "?"),
            "name": bt.get("name", ""),
            "price_usd": float(best.get("priceUsd", 0)) if best.get("priceUsd") else None,
            "price_native": float(best.get("priceNative", 0)) if best.get("priceNative") else None,
            "liquidity_usd": liq_all,
            "volume_24h": vol_all,
            "txns_buy": txns_buy,
            "txns_sell": txns_sell,
            "market_cap": float(best.get("marketCap", 0)) if best.get("marketCap") else None,
            "fdv": float(best.get("fdv", 0)) if best.get("fdv") else None,
            "pair_created_at": best.get("pairCreatedAt"),
            "price_change_24h": best.get("priceChange", {}).get("h24") if best.get("priceChange") else None,
            "circ_supply": circ_supply,
            "total_supply": total_supply,
            "url": best.get("url", ""),
        }
