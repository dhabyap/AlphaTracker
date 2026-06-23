"""Whale wallet tracker — crawl on-chain trades via BSC public RPC + Etherscan."""

import os
import time
import json
import requests
import concurrent.futures
from datetime import datetime, timezone

# BSC public RPC
BSC_RPC = os.environ.get("BSC_RPC_URL", "https://bsc-dataseed1.defibit.io")
DEX_BASE = "https://api.dexscreener.com/latest/dex"
CRAWL_INTERVAL = 1800  # 30 min between crawls
BLOCKS_PER_SCAN = 200   # ~10 min at 3s/block — fast enough for 30s crawl
PARALLEL_WORKERS = 15

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class WhaleTracker:
    def __init__(self, db):
        self.db = db
        self.bsc_key = os.environ.get("BSCSCAN_API_KEY", "")

    # ── Public API ────────────────────────────────────────────────────────

    def crawl_wallet(self, wallet: str) -> dict:
        """Scan recent BSC blocks + fallback APIs for wallet activity."""
        trades = []

        # 1) BSC RPC block scan
        rpc_trades = self._scan_blocks(wallet)
        trades.extend(rpc_trades)

        # 2) Etherscan (Ethereum) — works free with user's key
        if self.bsc_key:
            eth_trades = self._fetch_etherscan_eth(wallet)
            trades.extend(eth_trades)

        if not trades:
            print(f"[WhaleTracker] No trades for {wallet[:10]}...")
            self.db._exec(
                "UPDATE whales SET last_crawled_at = ? WHERE wallet_address = ?",
                (int(time.time()), wallet))
            return {"trades": 0, "source": "none"}

        # Deduplicate
        seen = set()
        unique = []
        for t in trades:
            key = t.get("tx_hash", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(t)
            elif not key:
                unique.append(t)

        # Save
        saved = 0
        for t in unique:
            try:
                self.db.add_whale_trade(
                    wallet=wallet,
                    contract=t.get("contract", ""),
                    symbol=t.get("symbol", "?"),
                    trade_type=t.get("type", "buy"),
                    amount=float(t.get("amount", 0)),
                    price_usd=float(t.get("price_usd", 0)),
                    value_usd=float(t.get("value_usd", 0)),
                    tx_hash=t.get("tx_hash", ""),
                    trade_at=int(t.get("timestamp", 0)),
                )
                saved += 1
            except Exception as e:
                print(f"[WhaleTracker] Error saving trade: {e}")

        # Recalculate stats
        stats = self.db.update_whale_stats(wallet)
        source = "rpc+eth" if self.bsc_key else "rpc"
        print(f"[WhaleTracker] {wallet[:10]}... → {saved} trades saved, {stats}")
        return {"trades": saved, "source": source, "stats": stats}

    def crawl_all(self) -> dict:
        """Crawl all tracked whales. Skip if crawled < 30 min ago."""
        whales = self.db.get_whales()
        now = int(time.time())
        results = {"crawled": 0, "skipped": 0, "errors": 0, "total_trades": 0}

        for w in whales:
            last = w.get("last_crawled_at", 0) or 0
            if now - last < CRAWL_INTERVAL:
                results["skipped"] += 1
                continue
            try:
                r = self.crawl_wallet(w["wallet_address"])
                results["crawled"] += 1
                results["total_trades"] += r.get("trades", 0)
            except Exception as e:
                print(f"[WhaleTracker] Error crawling {w['wallet_address'][:10]}...: {e}")
                results["errors"] += 1
                try:
                    self.db._exec(
                        "UPDATE whales SET last_crawled_at = ? WHERE wallet_address = ?",
                        (now, w["wallet_address"]))
                except:
                    pass
        return results

    # ── BSC RPC block scan (parallel) ─────────────────────────────────────

    def _scan_blocks(self, wallet: str) -> list:
        """Scan recent BSC blocks in parallel for wallet activity."""
        try:
            r = requests.post(BSC_RPC, json={
                "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1
            }, timeout=10)
            latest = int(r.json()["result"], 16)
        except Exception as e:
            print(f"[RPC] Can't get latest block: {e}")
            return []

        wallet_lower = wallet.lower()
        from_block = max(latest - BLOCKS_PER_SCAN, 0)

        def scan_block(bn):
            """Fetch one block, return (txs_involving_wallet, receipts_for_token_txs)."""
            try:
                r = requests.post(BSC_RPC, json={
                    "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
                    "params": [hex(bn), True], "id": 1
                }, timeout=15)
                data = r.json()
                if not data.get("result"):
                    return [], []
                txs = data["result"].get("transactions", [])
            except:
                return [], []

            direct_trades = []
            need_receipt = []
            for tx in txs:
                tx_from = (tx.get("from") or "").lower()
                tx_to = (tx.get("to") or "").lower()
                if tx_from == wallet_lower:
                    # Wallet sent — check if it's BNB transfer
                    value_wei = int(tx.get("value", "0x0"), 16)
                    if value_wei > 0:
                        direct_trades.append(self._make_bnb_trade(tx, wallet_lower, "sell"))
                    need_receipt.append(tx["hash"])
                elif tx_to == wallet_lower:
                    value_wei = int(tx.get("value", "0x0"), 16)
                    if value_wei > 0:
                        direct_trades.append(self._make_bnb_trade(tx, wallet_lower, "buy"))
                    # Also check receipt for token transfers
                    need_receipt.append(tx["hash"])

            # Get receipts for token transfers (batched)
            receipt_trades = []
            for tx_hash in need_receipt[:5]:  # limit per block
                try:
                    rt = requests.post(BSC_RPC, json={
                        "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
                        "params": [tx_hash], "id": 1
                    }, timeout=10)
                    receipt = rt.json().get("result")
                    if receipt:
                        parsed = self._parse_receipt_logs(receipt, wallet_lower, tx_hash)
                        receipt_trades.extend(parsed)
                except:
                    pass

            return direct_trades, receipt_trades

        trades = []
        blocks = list(range(latest, from_block - 1, -1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
            futures = {ex.submit(scan_block, bn): bn for bn in blocks}
            for f in concurrent.futures.as_completed(futures):
                try:
                    dt, rt = f.result()
                    trades.extend(dt)
                    trades.extend(rt)
                except:
                    pass

        print(f"[RPC] Scanned {len(blocks)} blocks for {wallet[:10]}... found {len(trades)} trades")
        return trades

    def _make_bnb_trade(self, tx: dict, wallet_lower: str, trade_type: str) -> dict:
        """Create a BNB trade entry from an RPC transaction."""
        value = int(tx.get("value", "0x0"), 16) / 1e18
        return {
            "contract": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            "symbol": "BNB",
            "type": trade_type,
            "amount": value,
            "price_usd": 0,
            "value_usd": 0,
            "tx_hash": tx.get("hash", ""),
            "timestamp": int(time.time()),
        }

    def _parse_receipt_logs(self, receipt: dict, wallet_lower: str, tx_hash: str) -> list:
        """Extract ERC-20 Transfer events from a transaction receipt."""
        trades = []
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                continue
            topic_to = "0x" + topics[2][-40:].lower()
            topic_from = "0x" + topics[1][-40:].lower()
            if topic_to != wallet_lower and topic_from != wallet_lower:
                continue

            data_hex = log.get("data", "0x")
            if data_hex in ("0x", ""):
                continue
            amount_raw = int(data_hex, 16)
            if amount_raw == 0:
                continue

            trade_type = "buy" if topic_to == wallet_lower else "sell"
            contract = log.get("address", "")
            trades.append({
                "contract": contract,
                "symbol": contract[:8],
                "type": trade_type,
                "amount": float(amount_raw),
                "price_usd": 0,
                "value_usd": 0,
                "tx_hash": tx_hash,
                "timestamp": int(time.time()),
            })
        return trades

    # ── Etherscan (Ethereum mainnet — FREE with user's key) ───────────────

    def _fetch_etherscan_eth(self, wallet: str) -> list:
        """Fetch ERC-20 token transfers on Ethereum mainnet (chainId=1 — free)."""
        if not self.bsc_key:
            return []
        trades = []
        try:
            r = requests.get("https://api.etherscan.io/v2/api", params={
                "chainid": 1,
                "module": "account",
                "action": "tokentx",
                "address": wallet,
                "sort": "desc",
                "apikey": self.bsc_key,
                "page": 1,
                "offset": 50,
            }, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("status") != "1":
                return []

            for tx in data.get("result", []):
                try:
                    ts = int(tx.get("timeStamp", 0))
                    symbol = tx.get("tokenSymbol", "?")
                    contract = tx.get("contractAddress", "")
                    decimals = int(tx.get("tokenDecimal", 18))
                    value = float(tx.get("value", 0)) / (10 ** decimals)
                    to_addr = tx.get("to", "").lower()
                    from_addr = tx.get("from", "").lower()
                    w_lower = wallet.lower()

                    if to_addr == w_lower:
                        trade_type = "buy"
                    elif from_addr == w_lower:
                        trade_type = "sell"
                    else:
                        continue

                    trades.append({
                        "contract": contract,
                        "symbol": symbol,
                        "type": trade_type,
                        "amount": value,
                        "price_usd": 0,
                        "value_usd": 0,
                        "tx_hash": tx.get("hash", ""),
                        "timestamp": ts,
                    })
                except:
                    continue
        except Exception as e:
            print(f"[Etherscan] ETH error: {e}")
        return trades

    # ── DexScreener price fill ────────────────────────────────────────────

    def fill_prices(self, wallet: str):
        """Fill missing price_usd for trades using DexScreener lookup."""
        trades = self.db.get_whale_trades(wallet, limit=200)
        contracts = set()
        for t in trades:
            if t.get("price_usd", 0) == 0 and t.get("token_contract"):
                contracts.add(t["token_contract"])

        prices = {}
        for c in list(contracts)[:20]:
            try:
                r = requests.get(f"{DEX_BASE}/search?q={c}", timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    pairs = r.json().get("pairs", [])
                    if pairs:
                        price = float(pairs[0].get("priceUsd", 0)) if pairs[0].get("priceUsd") else 0
                        prices[c] = price
            except:
                pass

        if not prices:
            return {"filled": 0}

        filled = 0
        for t in trades:
            c = t.get("token_contract", "")
            if c in prices and (t.get("price_usd", 0) or 0) == 0:
                amt = t.get("amount", 0) or 0
                pr = prices[c]
                val = amt * pr
                self.db._exec(
                    "UPDATE whale_trades SET price_usd = ?, value_usd = ? WHERE id = ?",
                    (pr, val, t["id"]))
                filled += 1

        if filled:
            self.db.update_whale_stats(wallet)

        return {"filled": filled}
