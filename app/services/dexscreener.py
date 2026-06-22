"""DexScreener API — fetch token data by contract address."""

import time
import requests
from typing import Optional

CACHE = {}
CACHE_TTL = 60  # seconds


class DexScreenerService:
    BASE = "https://api.dexscreener.com/latest/dex"

    def search(self, query: str) -> list:
        """Search tokens by contract address or symbol."""
        cached = CACHE.get(query)
        if cached and time.time() - cached["ts"] < CACHE_TTL:
            return cached["data"]

        try:
            url = f"{self.BASE}/search?q={query}"
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return []
            data = r.json()
            pairs = data.get("pairs", [])
            CACHE[query] = {"data": pairs, "ts": time.time()}
            return pairs
        except Exception:
            return []

    def get_by_chain_pair(self, chain: str, pair_addr: str) -> Optional[dict]:
        """Get single pair by chain + pair address."""
        try:
            url = f"{self.BASE}/pair/{chain}/{pair_addr}"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return r.json().get("pair")
        except Exception:
            pass
        return None

    def get_token_profile(self, contract: str) -> dict:
        """Aggregate DexScreener data for a token across pairs."""
        pairs = self.search(contract)
        if not pairs:
            return {}

        # Merge data from all pairs
        result = {
            "chain": pairs[0].get("chainId", "?"),
            "dex": pairs[0].get("dexId", "?"),
            "symbol": pairs[0].get("baseToken", {}).get("symbol", "?"),
            "name": pairs[0].get("baseToken", {}).get("name", ""),
            "price_usd": None,
            "price_native": None,
            "liquidity_usd": 0,
            "volume_24h": 0,
            "txns_24h_buy": 0,
            "txns_24h_sell": 0,
            "market_cap": None,
            "fdv": None,
            "pair_created_at": None,
            "price_change_24h": None,
            "url": pairs[0].get("url", ""),
        }

        # Aggregate across pairs
        for p in pairs:
            liq = p.get("liquidity", {}).get("usd", 0) or 0
            result["liquidity_usd"] += liq

            vol = p.get("volume", {}).get("h24", 0) or 0
            result["volume_24h"] += vol

            txns = p.get("txns", {}).get("h24", {})
            result["txns_24h_buy"] += txns.get("buys", 0)
            result["txns_24h_sell"] += txns.get("sells", 0)

            # Take first non-null price
            if result["price_usd"] is None and p.get("priceUsd"):
                result["price_usd"] = float(p["priceUsd"])
            if result["price_native"] is None and p.get("priceNative"):
                result["price_native"] = float(p["priceNative"])
            if result["market_cap"] is None and p.get("marketCap"):
                result["market_cap"] = float(p["marketCap"])
            if result["fdv"] is None and p.get("fdv"):
                result["fdv"] = float(p["fdv"])
            if result["pair_created_at"] is None and p.get("pairCreatedAt"):
                result["pair_created_at"] = p["pairCreatedAt"]
            if result["price_change_24h"] is None and p.get("priceChange", {}).get("h24"):
                result["price_change_24h"] = p["priceChange"]["h24"]

        return result
