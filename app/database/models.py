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
    def __init__(self):
        self._init_db()
        self._seed()

    def _exec(self, sql, params=None):
        conn = get_conn()
        try:
            cur = conn.cursor()
            if IS_MYSQL:
                cur.execute(sql.replace("?", "%s"), params or ())
            else:
                cur.execute(sql, params or ())
            conn.commit()
            return cur
        finally:
            conn.close()

    def _fetch(self, sql, params=None):
        conn = get_conn()
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

    def _migrate_ai_columns(self):
        """Add ai_recommendation columns to tokens table if missing."""
        try:
            if IS_MYSQL:
                # Check if column exists
                rows = self._fetch(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'tokens' AND COLUMN_NAME = 'ai_recommendation'",
                    (cfg["database"],),
                )
                if not rows:
                    self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation JSON")
                    self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation_at INT DEFAULT 0")
            else:
                # SQLite: use PRAGMA to check columns
                conn = get_conn()
                try:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_info(tokens)")
                    cols = [row["name"] for row in cur.fetchall()]
                    if "ai_recommendation" not in cols:
                        self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation TEXT DEFAULT ''")
                        self._exec("ALTER TABLE tokens ADD COLUMN ai_recommendation_at INTEGER DEFAULT 0")
                finally:
                    conn.close()
        except Exception as e:
            print(f"[DB] AI migration skipped: {e}")

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
