"""Whale Discovery — fast BSC block scan + immediate trade import."""

import os
import time
import threading
import requests
import concurrent.futures
from collections import defaultdict

BSC_RPC = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed1.defibit.io")

DEX_ROUTERS = {
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap v2
    "0x05ff2b0db69458a0750badebc4f9e13add608c7f",  # PancakeSwap v1
}
KNOWN = DEX_ROUTERS | {
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",
    "0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503",
    "0x0000000000007f150bd6f54c40a34d7c3d5e9f56",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000001004",
}


class WhaleDiscovery:
    def __init__(self, db):
        self.db = db

    def discover(self, limit: int = 10) -> list:
        tracked = {w["wallet_address"].lower() for w in self.db.get_whales()}
        candidates = self._scan_traders(tracked)

        candidates.sort(key=lambda x: (-x["is_trader"], -x["score"]))
        print(f"[Discovery] {len(candidates)} candidates ({sum(1 for c in candidates if c['is_trader'])} traders)")
        return candidates[:limit]

    def import_trades(self, wallet: str, tx_hashes: list) -> int:
        """Import trades from specific tx hashes immediately."""
        imported = 0
        for tx_hash in tx_hashes[:50]:
            try:
                r = requests.post(BSC_RPC, json={
                    "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
                    "params": [tx_hash], "id": 1
                }, timeout=10)
                receipt = r.json().get("result")
                if not receipt:
                    continue
                self.db.save_whale_trade(wallet, tx_hash, receipt)
                imported += 1
            except:
                continue
        return imported

    def _scan_traders(self, tracked: set) -> list:
        """Scan last 30 BSC blocks for DEX traders + BNB transfers."""
        try:
            r = requests.post(BSC_RPC, json={
                "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1
            }, timeout=8)
            latest = int(r.json()["result"], 16)
        except:
            return []

        from_block = max(latest - 30, 0)
        stats = defaultdict(lambda: {"tx": 0, "dex": 0, "val": 0.0, "txs": []})

        def scan(bn):
            local = defaultdict(lambda: {"tx": 0, "dex": 0, "val": 0.0, "txs": []})
            try:
                r = requests.post(BSC_RPC, json={
                    "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                    "params": [hex(bn), True], "id": 1
                }, timeout=10)
                data = r.json().get("result")
                if not data:
                    return local
                for tx in data.get("transactions", []):
                    frm = (tx.get("from") or "").lower()
                    to = (tx.get("to") or "").lower()
                    val = int(tx.get("value", "0x0"), 16) / 1e18
                    if not frm or frm in tracked or frm in KNOWN or frm.startswith("0x0000000000"):
                        continue
                    if to in DEX_ROUTERS or val >= 0.1:
                        s = local[frm]
                        s["tx"] += 1
                        if to in DEX_ROUTERS:
                            s["dex"] += 1
                        s["val"] += val
                        s["txs"].append(tx.get("hash", ""))
            except:
                pass
            return local

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(scan, bn) for bn in range(from_block, latest + 1)]
            for f in concurrent.futures.as_completed(futures):
                try:
                    for addr, s in f.result().items():
                        t = stats[addr]
                        t["tx"] += s["tx"]
                        t["dex"] += s["dex"]
                        t["val"] += s["val"]
                        t["txs"].extend(s["txs"])
                except:
                    pass

        result = []
        for addr, s in stats.items():
            if s["tx"] == 0:
                continue
            score = s["dex"] * 50 + s["val"] * 10 + s["tx"] * 2
            result.append({
                "wallet": addr,
                "tx_count": s["tx"],
                "dex_count": s["dex"],
                "total_value_eth": round(s["val"], 4),
                "score": round(score, 1),
                "is_trader": s["dex"] > 0,
                "tokens": [],
                "source": "dex" if s["dex"] > 0 else "bnb_tx",
                "tx_hashes": s["txs"][:5],
            })
        result.sort(key=lambda x: (-x["is_trader"], -x["score"]))
        return result[:10]
