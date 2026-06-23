"""Database — SQLite (default) or MySQL (Laragon)."""

import json
import time
import os

from app.database.config import get_cfg

cfg = get_cfg()
IS_MYSQL = cfg["driver"] == "mysql"


def get_conn():
    if IS_MYSQL:
        import pymysql

        return pymysql.connect(
            host=cfg["host"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    else:
        import sqlite3

        path = cfg["database"]
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


class Database:
    _conn = None  # persistent MySQL connection

    def _get_pconn(self):
        """Get persistent connection (MySQL) or temp (SQLite)."""
        if IS_MYSQL:
            if self._conn is None or not self._conn.open:
                import pymysql
                self._conn = pymysql.connect(
                    host=cfg["host"], user=cfg["user"], password=cfg["password"],
                    database=cfg["database"], charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=True, connect_timeout=30,
                )
            return self._conn
        return get_conn()

    def __init__(self):
        self._init_db()
        self._seed()

    def _exec(self, sql, params=None):
        conn = self._get_pconn()
        try:
            cur = conn.cursor()
            if IS_MYSQL:
                cur.execute(sql.replace("?", "%s"), params or ())
                return cur
            else:
                cur.execute(sql, params or ())
                conn.commit()
                return cur
        finally:
            if not IS_MYSQL:
                conn.close()

    def _fetch(self, sql, params=None):
        conn = self._get_pconn()
        try:
            cur = conn.cursor()
            if IS_MYSQL:
                cur.execute(sql.replace("?", "%s"), params or ())
            else:
                cur.execute(sql, params or ())
            rows = cur.fetchall()
            if IS_MYSQL:
                return rows
            return [dict(r) for r in rows]
        finally:
            if not IS_MYSQL:
                conn.close()

    def _init_db(self):
        if IS_MYSQL:
            self._exec("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    contract VARCHAR(255) UNIQUE NOT NULL,
                    symbol VARCHAR(50) NOT NULL,
                    name VARCHAR(255) DEFAULT '',
                    chain VARCHAR(20) DEFAULT 'bsc',
                    added_at INT NOT NULL DEFAULT (UNIX_TIMESTAMP()),
                    latest_analysis JSON,
                    latest_score FLOAT DEFAULT 0,
                    notes TEXT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            self._exec("""
                CREATE TABLE IF NOT EXISTS analysis_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    contract VARCHAR(255) NOT NULL,
                    analysis JSON NOT NULL,
                    created_at INT NOT NULL DEFAULT (UNIX_TIMESTAMP())
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            # MySQL doesn't support CREATE INDEX IF NOT EXISTS
            for idx in [
                ("idx_tokens_score", "tokens(latest_score DESC)"),
                ("idx_tokens_contract", "tokens(contract)"),
                ("idx_history_contract", "analysis_history(contract)"),
            ]:
                try:
                    self._exec(f"CREATE INDEX {idx[0]} ON {idx[1]}")
                except:
                    pass  # Index likely exists
        else:
            self._exec("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    chain TEXT DEFAULT 'bsc',
                    added_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    latest_analysis TEXT,
                    latest_score REAL DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
            """)
            self._exec("""
                CREATE TABLE IF NOT EXISTS analysis_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract TEXT NOT NULL,
                    analysis TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                )
            """)
            self._exec("CREATE INDEX IF NOT EXISTS idx_tokens_score ON tokens(latest_score DESC)")
            self._exec("CREATE INDEX IF NOT EXISTS idx_tokens_contract ON tokens(contract)")
            self._exec("CREATE INDEX IF NOT EXISTS idx_history_contract ON analysis_history(contract)")
        # Migrate AI columns
        self._migrate_whale_columns()
        self._migrate_ai_columns()

    def _seed(self):
        seeds = [
            ("0x4d41a5d412f4ef44a35b9f53b06db65ede249493", "QAIT", "QAIT", "bsc"),
            ("0x8fce7206e3043dd360f115afa956ee31b90b787c", "STAR", "Starpower Network", "bsc"),
        ]
        for contract, symbol, name, chain in seeds:
            existing = self._fetch("SELECT id FROM tokens WHERE contract = ?", (contract,))
            if not existing:
                self._exec(
                    "INSERT INTO tokens (contract, symbol, name, chain) VALUES (?, ?, ?, ?)",
                    (contract, symbol, name, chain),
                )

    def add_token(self, contract: str, symbol: str, name: str, chain: str = "bsc") -> int:
        existing = self._fetch("SELECT id FROM tokens WHERE contract = ?", (contract,))
        if existing:
            return existing[0]["id"]
        self._exec(
            "INSERT INTO tokens (contract, symbol, name, chain) VALUES (?, ?, ?, ?)",
            (contract, symbol, name, chain),
        )
        row = self._fetch("SELECT id FROM tokens WHERE contract = ?", (contract,))
        return row[0]["id"] if row else 0

    def get_token(self, contract: str) -> dict | None:
        rows = self._fetch("SELECT * FROM tokens WHERE contract = ?", (contract,))
        if not rows:
            return None
        t = rows[0]
        if t.get("latest_analysis"):
            t["latest_analysis"] = json.loads(t["latest_analysis"]) if isinstance(t["latest_analysis"], str) else t["latest_analysis"]
        return t

    def get_all_tokens(self, sort: str = "score", limit: int = 50) -> list:
        sort_map = {"score": "latest_score DESC", "volume": "latest_score DESC", "mc": "latest_score DESC", "age": "id DESC", "newest": "id DESC"}
        order = sort_map.get(sort, "latest_score DESC")
        rows = self._fetch(f"SELECT * FROM tokens ORDER BY {order} LIMIT ?", (limit,))
        for t in rows:
            if t.get("latest_analysis"):
                t["latest_analysis"] = json.loads(t["latest_analysis"]) if isinstance(t["latest_analysis"], str) else t["latest_analysis"]
        return rows

    def save_analysis(self, contract: str, analysis: dict):
        analysis_json = json.dumps(analysis)
        self._exec(
            "UPDATE tokens SET latest_analysis = ?, latest_score = ? WHERE contract = ?",
            (analysis_json, analysis.get("score", 0), contract),
        )
        self._exec(
            "INSERT INTO analysis_history (contract, analysis, created_at) VALUES (?, ?, ?)",
            (contract, analysis_json, int(time.time())),
        )

    def update_token_notes(self, contract: str, notes_json: str):
        """Store manual fundamental data (holders, supply, etc) as JSON."""
        self._exec("UPDATE tokens SET notes = ? WHERE contract = ?", (notes_json, contract))

    def get_history(self, contract: str, limit: int = 10) -> list:
        rows = self._fetch(
            "SELECT * FROM analysis_history WHERE contract = ? ORDER BY id DESC LIMIT ?",
            (contract, limit),
        )
        for h in rows:
            if h.get("analysis"):
                h["analysis"] = json.loads(h["analysis"]) if isinstance(h["analysis"], str) else h["analysis"]
        return rows

    def count_tokens(self) -> int:
        rows = self._fetch("SELECT COUNT(*) as cnt FROM tokens")
        return rows[0]["cnt"] if rows else 0

    def remove_token(self, contract: str) -> bool:
        self._exec("DELETE FROM analysis_history WHERE contract = ?", (contract,))
        cur = self._exec("DELETE FROM tokens WHERE contract = ?", (contract,))
        return True

    def set_notes(self, contract: str, notes: dict):
        self._exec(
            "UPDATE tokens SET notes = ? WHERE contract = ?",
            (json.dumps(notes), contract),
        )

    def _migrate_whale_columns(self):
        """Add whale_trades table + last_crawled_at column."""
        try:
            if IS_MYSQL:
                # Add last_crawled_at to whales
                rows = self._fetch("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'whales' AND COLUMN_NAME = 'last_crawled_at'", (cfg["database"],))
                if not rows:
                    try:
                        self._exec("ALTER TABLE whales ADD COLUMN last_crawled_at INT DEFAULT 0")
                    except:
                        pass
                # Create whale_trades table
                rows2 = self._fetch("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'whale_trades'", (cfg["database"],))
                if not rows2:
                    self._exec("""CREATE TABLE whale_trades (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        wallet_address VARCHAR(42) NOT NULL,
                        token_contract VARCHAR(255) NOT NULL,
                        token_symbol VARCHAR(50) DEFAULT '',
                        trade_type VARCHAR(10) DEFAULT '',
                        amount DOUBLE DEFAULT 0,
                        price_usd DOUBLE DEFAULT 0,
                        value_usd DOUBLE DEFAULT 0,
                        tx_hash VARCHAR(255) DEFAULT '',
                        trade_at INT DEFAULT 0,
                        pnl DOUBLE DEFAULT NULL,
                        created_at INT DEFAULT (UNIX_TIMESTAMP())
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
                    for idx in ["idx_wt_wallet", "idx_wt_contract"]:
                        try: self._exec(f"CREATE INDEX {idx} ON whale_trades(wallet_address)")
                        except: pass
            else:
                # SQLite
                conn = get_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_info(whales)")
                    cols = [row["name"] for row in cur.fetchall()]
                    if "last_crawled_at" not in cols:
                        self._exec("ALTER TABLE whales ADD COLUMN last_crawled_at INTEGER DEFAULT 0")
                finally:
                    conn.close()
                self._exec("""CREATE TABLE IF NOT EXISTS whale_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    token_contract TEXT NOT NULL,
                    token_symbol TEXT DEFAULT '',
                    trade_type TEXT DEFAULT '',
                    amount REAL DEFAULT 0,
                    price_usd REAL DEFAULT 0,
                    value_usd REAL DEFAULT 0,
                    tx_hash TEXT DEFAULT '',
                    trade_at INTEGER DEFAULT 0,
                    pnl REAL DEFAULT NULL,
                    created_at INTEGER DEFAULT (unixepoch())
                )""")
                self._exec("CREATE INDEX IF NOT EXISTS idx_wt_wallet ON whale_trades(wallet_address)")
                self._exec("CREATE INDEX IF NOT EXISTS idx_wt_contract ON whale_trades(token_contract)")
        except Exception as e:
            print(f"[DB] Whale migration: {e}")

    def _migrate_ai_columns(self):
        """Add ai_recommendation, portfolio, and whale tracking columns."""
        try:
            if IS_MYSQL:
                # Existing migrations
                rows = self._fetch("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'tokens' AND COLUMN_NAME = 'ai_recommendation'", (cfg["database"],))
                if not rows:
                    self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation JSON")
                    self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation_at INT DEFAULT 0")
                rows2 = self._fetch("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'tokens' AND COLUMN_NAME = 'buy_price'", (cfg["database"],))
                if not rows2:
                    self._exec("ALTER TABLE tokens ADD COLUMN buy_price DOUBLE DEFAULT 0")
                    self._exec("ALTER TABLE tokens ADD COLUMN buy_amount DOUBLE DEFAULT 0")
                    self._exec("ALTER TABLE tokens ADD COLUMN buy_date VARCHAR(20) DEFAULT ''")
                
                # Whale tracking
                rows3 = self._fetch("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'whales'", (cfg["database"],))
                if not rows3:
                    self._exec("""CREATE TABLE whales (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        wallet_address VARCHAR(42) UNIQUE,
                        label VARCHAR(50),
                        win_rate DOUBLE DEFAULT 0,
                        last_profit DOUBLE DEFAULT 0,
                        added_at INT
                    )""")
            else:
                conn = get_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_info(tokens)")
                    cols = [row["name"] for row in cur.fetchall()]
                    if "ai_recommendation" not in cols:
                        self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation TEXT DEFAULT ''")
                        self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation_at INTEGER DEFAULT 0")
                    if "buy_price" not in cols:
                        self._exec("ALTER TABLE tokens ADD COLUMN buy_price REAL DEFAULT 0")
                        self._exec("ALTER TABLE tokens ADD COLUMN buy_amount REAL DEFAULT 0")
                        self._exec("ALTER TABLE tokens ADD COLUMN buy_date TEXT DEFAULT ''")
                    
                    self._exec("CREATE TABLE IF NOT EXISTS whales (id INTEGER PRIMARY KEY, wallet_address TEXT UNIQUE, label TEXT, win_rate REAL, last_profit REAL, added_at INTEGER)")
                finally:
                    conn.close()
        except Exception as e:
            print(f"[DB] Migration skipped: {e}")

    def get_ai_recommendation(self, contract: str, max_age_seconds: int = 3600) -> dict | None:
        """Get cached AI recommendation if fresh (within max_age_seconds)."""
        rows = self._fetch(
            "SELECT ai_recommendation, ai_recommendation_at FROM tokens WHERE contract = ?",
            (contract,),
        )
        if not rows or not rows[0].get("ai_recommendation"):
            return None
        row = rows[0]
        cached_at = row.get("ai_recommendation_at", 0)
        if time.time() - cached_at > max_age_seconds:
            return None  # Expired
        try:
            data = json.loads(row["ai_recommendation"]) if isinstance(row["ai_recommendation"], str) else row["ai_recommendation"]
            data["_cached"] = True
            data["_cached_at"] = cached_at
            return data
        except (json.JSONDecodeError, TypeError):
            return None

    def save_ai_recommendation(self, contract: str, ai_data: dict):
        """Save AI recommendation to DB cache."""
        ai_json = json.dumps(ai_data, ensure_ascii=False)
        self._exec(
            "UPDATE tokens SET ai_recommendation = ?, ai_recommendation_at = ? WHERE contract = ?",
            (ai_json, int(time.time()), contract),
        )

    def set_portfolio(self, contract: str, buy_price: float, buy_amount: float, buy_date: str = ""):
        """Set portfolio entry (buy price, amount, date) for a token."""
        if not buy_date:
            buy_date = time.strftime("%Y-%m-%d")
        self._exec(
            "UPDATE tokens SET buy_price = ?, buy_amount = ?, buy_date = ? WHERE contract = ?",
            (buy_price, buy_amount, buy_date, contract),
        )

    def clear_portfolio(self, contract: str):
        """Remove portfolio entry for a token."""
        self._exec(
            "UPDATE tokens SET buy_price = 0, buy_amount = 0, buy_date = '' WHERE contract = ?",
            (contract,),
        )

    def get_portfolio_summary(self) -> dict:
        """Get portfolio summary with P&L for all tokens with buy entries."""
        tokens = self.get_all_tokens(limit=200)
        entries = []
        total_cost = 0
        total_value = 0
        for t in tokens:
            bp = t.get("buy_price", 0) or 0
            ba = t.get("buy_amount", 0) or 0
            if bp <= 0 or ba <= 0:
                continue
            analysis = t.get("latest_analysis") or {}
            if isinstance(analysis, str):
                try:
                    analysis = json.loads(analysis)
                except:
                    analysis = {}
            current_price = analysis.get("metrics", {}).get("price_usd", 0) or 0
            cost = bp * ba
            value = current_price * ba
            pnl = value - cost
            pnl_pct = ((current_price - bp) / bp * 100) if bp > 0 else 0
            total_cost += cost
            total_value += value
            entries.append({
                "contract": t["contract"],
                "symbol": t["symbol"],
                "buy_price": bp,
                "buy_amount": ba,
                "buy_date": t.get("buy_date", ""),
                "current_price": current_price,
                "cost": cost,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })
        entries.sort(key=lambda x: -abs(x["pnl"]))
        return {
            "entries": entries,
            "total_cost": total_cost,
            "total_value": total_value,
            "total_pnl": total_value - total_cost,
            "total_pnl_pct": ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0,
        }

    def add_whale(self, wallet: str, label: str = ""):
        self._exec(
            "INSERT INTO whales (wallet_address, label, added_at) VALUES (?, ?, ?)",
            (wallet, label, int(time.time())),
        )

    def get_whales(self) -> list:
        return self._fetch("SELECT * FROM whales ORDER BY id DESC")

    def get_whale(self, wallet: str) -> dict | None:
        rows = self._fetch("SELECT * FROM whales WHERE wallet_address = ?", (wallet,))
        return rows[0] if rows else None

    # ── WHALE TRADES ────────────────────────────────────────────────────────────

    def add_whale_trade(self, wallet: str, contract: str, symbol: str, trade_type: str,
                        amount: float, price_usd: float, value_usd: float,
                        tx_hash: str = "", trade_at: int = 0):
        """Record a whale trade. Skip duplicate tx_hash."""
        if tx_hash:
            existing = self._fetch("SELECT id FROM whale_trades WHERE tx_hash = ? AND wallet_address = ?",
                                    (tx_hash, wallet))
            if existing:
                return existing[0]["id"]
        self._exec("""INSERT INTO whale_trades
            (wallet_address, token_contract, token_symbol, trade_type, amount, price_usd, value_usd, tx_hash, trade_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (wallet, contract, symbol, trade_type, amount, price_usd, value_usd, tx_hash, trade_at))
        rows = self._fetch("SELECT id FROM whale_trades WHERE wallet_address = ? ORDER BY id DESC LIMIT 1", (wallet,))
        return rows[0]["id"] if rows else 0

    def get_whale_trades(self, wallet: str, limit: int = 50) -> list:
        return self._fetch(
            "SELECT * FROM whale_trades WHERE wallet_address = ? ORDER BY trade_at DESC LIMIT ?",
            (wallet, limit))

    def get_all_whale_trades(self, limit: int = 100) -> list:
        return self._fetch("SELECT * FROM whale_trades ORDER BY trade_at DESC LIMIT ?", (limit,))

    def get_trades_by_token(self, contract: str, limit: int = 50) -> list:
        """Get whale trades for a specific token contract."""
        return self._fetch(
            "SELECT wt.*, w.label as whale_label FROM whale_trades wt "
            "LEFT JOIN whales w ON w.wallet_address COLLATE utf8mb4_0900_ai_ci = wt.wallet_address "
            "WHERE wt.token_contract = %s ORDER BY wt.trade_at DESC LIMIT %s",
            (contract, limit))

    def update_whale_stats(self, wallet: str):
        """Recalculate and update win_rate, last_profit, last_crawled_at for a whale."""
        # Get all trades grouped by token contract
        trades = self._fetch(
            "SELECT * FROM whale_trades WHERE wallet_address = ? ORDER BY token_contract, trade_at",
            (wallet,))

        if not trades:
            self._exec("UPDATE whales SET win_rate = 0, last_profit = 0, last_crawled_at = ? WHERE wallet_address = ?",
                        (int(time.time()), wallet))
            return

        # Group by token and calculate P&L
        token_groups = {}
        for t in trades:
            c = t["token_contract"]
            if c not in token_groups:
                token_groups[c] = {"buys": [], "sells": [], "symbol": t.get("token_symbol", "?")}
            if t["trade_type"] == "buy" or t["trade_type"] == "incoming":
                token_groups[c]["buys"].append(t)
            else:
                token_groups[c]["sells"].append(t)

        # Calculate closed trades (tokens with both buys and sells)
        total_trades = 0
        wins = 0
        total_pnl = 0
        last_pnl = 0

        for c, g in token_groups.items():
            buys = g["buys"]
            sells = g["sells"]
            if not buys or not sells:
                continue

            # Use total value_usd instead of avg price (more accurate with DexScreener data)
            buy_value = sum(b["value_usd"] for b in buys if b["value_usd"] and b["value_usd"] > 0)
            sell_value = sum(s["value_usd"] for s in sells if s["value_usd"] and s["value_usd"] > 0)

            if buy_value > 0 or sell_value > 0:
                trade_pnl = sell_value - buy_value
                is_win = sell_value > buy_value
            else:
                # Fallback: count unique days with activity as proxy
                trade_pnl = 0
                is_win = False

            total_pnl += trade_pnl
            last_pnl = trade_pnl

            if is_win:
                wins += 1
            total_trades += 1

        win_rate = wins / max(total_trades, 1)

        self._exec(
            "UPDATE whales SET win_rate = ?, last_profit = ?, last_crawled_at = ? WHERE wallet_address = ?",
            (win_rate, last_pnl, int(time.time()), wallet))

        return {"wins": wins, "total": total_trades, "win_rate": win_rate, "total_pnl": total_pnl}

    def delete_whale(self, wallet: str):
        """Remove a whale and all its trades from tracking."""
        self._exec("DELETE FROM whales WHERE wallet_address = ?", (wallet,))
        self._exec("DELETE FROM whale_trades WHERE wallet_address = ?", (wallet,))

    def delete_whale_trades(self, wallet: str):
        """Delete all trades for a whale wallet."""
        self._exec("DELETE FROM whale_trades WHERE wallet_address = ?", (wallet,))
