"""SQLite repository (WAL mode, restart-safe). Thread-safe via a lock; calls are short."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, chat_id INTEGER, created_at INTEGER DEFAULT (strftime('%s','now')), settings TEXT);
CREATE TABLE IF NOT EXISTS accounts(user_id INTEGER, chain TEXT, balance REAL, start_balance REAL, PRIMARY KEY(user_id, chain));
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chain TEXT, token TEXT, symbol TEXT,
  source TEXT, trader TEXT, tokens REAL, cost_native REAL, initial_tokens REAL, initial_cost REAL, realized REAL DEFAULT 0,
  fees REAL DEFAULT 0, opened_at INTEGER, closed_at INTEGER, status TEXT DEFAULT 'open', meta TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_pos_user ON positions(user_id, chain, status);
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chain TEXT, position_id INTEGER, side TEXT,
  token TEXT, symbol TEXT, source TEXT, trader TEXT, tokens REAL, native REAL, price REAL, leader_price REAL, slippage_pct REAL,
  impact_pct REAL, fee_native REAL, gas_native REAL, pnl_native REAL, ts INTEGER, leader_sig TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_fills_user ON fills(user_id, ts);
CREATE TABLE IF NOT EXISTS skipped(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chain TEXT, token TEXT, symbol TEXT, trader TEXT, reason TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS following(user_id INTEGER, chain TEXT, wallet TEXT, label TEXT, since INTEGER, PRIMARY KEY(user_id, chain));
CREATE TABLE IF NOT EXISTS trader_state(user_id INTEGER, chain TEXT, wallet TEXT, consec_losses INTEGER DEFAULT 0,
  benched_until INTEGER DEFAULT 0, bench_reason TEXT, PRIMARY KEY(user_id, chain, wallet));
CREATE TABLE IF NOT EXISTS leaderboard(chain TEXT, wallet TEXT, label TEXT, pnl REAL, wins INTEGER, losses INTEGER, trades INTEGER,
  score REAL, source TEXT, rank INTEGER, flagged TEXT, updated INTEGER, PRIMARY KEY(chain, wallet));
CREATE TABLE IF NOT EXISTS leader_events(chain TEXT, signature TEXT, wallet TEXT, token TEXT, side TEXT, ts INTEGER,
  token_amount REAL, native_amount REAL, symbol TEXT, PRIMARY KEY(chain, signature, wallet, token, side));
CREATE INDEX IF NOT EXISTS ix_le_ts ON leader_events(chain, ts);
CREATE TABLE IF NOT EXISTS leader_holdings(chain TEXT, wallet TEXT, token TEXT, amount REAL, PRIMARY KEY(chain, wallet, token));
CREATE TABLE IF NOT EXISTS funders(chain TEXT, wallet TEXT, funder TEXT, checked_at INTEGER, PRIMARY KEY(chain, wallet));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


class SqliteRepo:
    def __init__(self, path: str = "copycat.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # helpers
    def _q(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _x(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    @staticmethod
    def _pos(r: dict[str, Any] | None) -> dict[str, Any] | None:
        if r is not None:
            r["meta"] = json.loads(r.get("meta") or "{}")
        return r

    # users
    def upsert_user(self, user_id: int, chat_id: int, default_settings: dict[str, Any]) -> bool:
        exists = self._q("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if exists:
            self._x("UPDATE users SET chat_id=? WHERE user_id=?", (chat_id, user_id))
            return False
        self._x("INSERT INTO users(user_id, chat_id, settings) VALUES(?,?,?)", (user_id, chat_id, json.dumps(default_settings)))
        return True

    def get_settings(self, user_id: int) -> dict[str, Any]:
        r = self._q("SELECT settings FROM users WHERE user_id=?", (user_id,))
        return json.loads(r[0]["settings"]) if r else {}

    def save_settings(self, user_id: int, settings: dict[str, Any]) -> None:
        self._x("UPDATE users SET settings=? WHERE user_id=?", (json.dumps(settings), user_id))

    def list_users(self) -> list[dict[str, Any]]:
        return self._q("SELECT user_id, chat_id FROM users")

    # accounts
    def ensure_account(self, user_id: int, chain: str, start: float) -> None:
        self._x("INSERT OR IGNORE INTO accounts(user_id, chain, balance, start_balance) VALUES(?,?,?,?)", (user_id, chain, start, start))

    def get_account(self, user_id: int, chain: str) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM accounts WHERE user_id=? AND chain=?", (user_id, chain))
        return r[0] if r else None

    def get_balance(self, user_id: int, chain: str) -> float:
        a = self.get_account(user_id, chain)
        return float(a["balance"]) if a else 0.0

    def add_balance(self, user_id: int, chain: str, delta: float) -> float:
        with self.lock:
            self._x("UPDATE accounts SET balance=balance+? WHERE user_id=? AND chain=?", (delta, user_id, chain))
            return self.get_balance(user_id, chain)

    # positions
    def open_position(self, **kw: Any) -> int:
        kw = dict(kw)
        kw["meta"] = json.dumps(kw.get("meta") or {})
        cols = ",".join(kw)
        cur = self._x(f"INSERT INTO positions({cols}) VALUES({','.join('?' * len(kw))})", list(kw.values()))
        return int(cur.lastrowid)

    def get_position(self, pid: int) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM positions WHERE id=?", (pid,))
        return self._pos(r[0]) if r else None

    def find_open_position(self, user_id: int, chain: str, token: str, source: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM positions WHERE user_id=? AND chain=? AND token=? AND status='open'"
        args: list[Any] = [user_id, chain, token]
        if source:
            sql += " AND source=?"
            args.append(source)
        r = self._q(sql + " ORDER BY id LIMIT 1", args)
        return self._pos(r[0]) if r else None

    def open_positions(self, user_id: int | None = None, chain: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM positions WHERE status='open'", []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if chain:
            sql += " AND chain=?"
            args.append(chain)
        return [self._pos(r) for r in self._q(sql + " ORDER BY id", args)]  # type: ignore[misc]

    def update_position(self, pid: int, **fields: Any) -> None:
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"])
        sets = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE positions SET {sets} WHERE id=?", [*fields.values(), pid])

    def add_fill(self, **kw: Any) -> int:
        cols = ",".join(kw)
        return int(self._x(f"INSERT INTO fills({cols}) VALUES({','.join('?' * len(kw))})", list(kw.values())).lastrowid)

    def fills(self, user_id: int, since_ts: int = 0, chain: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM fills WHERE user_id=? AND ts>=?", [user_id, since_ts]
        if chain:
            sql += " AND chain=?"
            args.append(chain)
        return self._q(sql + " ORDER BY ts, id", args)

    def closed_positions(self, user_id: int, since_ts: int = 0, chain: str | None = None, trader: str | None = None,
                         source: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM positions WHERE user_id=? AND status='closed' AND closed_at>=?", [user_id, since_ts]
        for col, val in (("chain", chain), ("trader", trader), ("source", source)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        sql += " ORDER BY closed_at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._pos(r) for r in self._q(sql, args)]  # type: ignore[misc]

    def add_skip(self, user_id: int, chain: str, token: str, symbol: str | None, trader: str | None, reason: str, ts: int) -> None:
        self._x("INSERT INTO skipped(user_id, chain, token, symbol, trader, reason, ts) VALUES(?,?,?,?,?,?,?)",
                (user_id, chain, token, symbol, trader, reason, ts))

    def skips(self, user_id: int, since_ts: int = 0) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM skipped WHERE user_id=? AND ts>=? ORDER BY ts DESC, id DESC", (user_id, since_ts))

    # traders
    def get_following(self, user_id: int, chain: str) -> dict[str, Any] | None:
        r = self._q("SELECT * FROM following WHERE user_id=? AND chain=?", (user_id, chain))
        return r[0] if r and r[0]["wallet"] else None

    def set_following(self, user_id: int, chain: str, wallet: str | None, label: str | None, ts: int) -> None:
        self._x("INSERT OR REPLACE INTO following(user_id, chain, wallet, label, since) VALUES(?,?,?,?,?)", (user_id, chain, wallet, label, ts))

    def followers_of(self, chain: str, wallet: str) -> list[int]:
        return [r["user_id"] for r in self._q("SELECT user_id FROM following WHERE chain=? AND wallet=?", (chain, wallet))]

    def all_followed(self, chain: str) -> list[str]:
        return [r["wallet"] for r in self._q("SELECT DISTINCT wallet FROM following WHERE chain=? AND wallet IS NOT NULL", (chain,))]

    def trader_state(self, user_id: int, chain: str, wallet: str) -> dict[str, Any]:
        r = self._q("SELECT * FROM trader_state WHERE user_id=? AND chain=? AND wallet=?", (user_id, chain, wallet))
        return r[0] if r else {"user_id": user_id, "chain": chain, "wallet": wallet, "consec_losses": 0, "benched_until": 0, "bench_reason": None}

    def save_trader_state(self, user_id: int, chain: str, wallet: str, **fields: Any) -> None:
        st = self.trader_state(user_id, chain, wallet)
        st.update(fields)
        self._x("INSERT OR REPLACE INTO trader_state(user_id, chain, wallet, consec_losses, benched_until, bench_reason) VALUES(?,?,?,?,?,?)",
                (user_id, chain, wallet, st["consec_losses"], st["benched_until"], st["bench_reason"]))

    def benched(self, user_id: int, chain: str, now: int) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM trader_state WHERE user_id=? AND chain=? AND benched_until>?", (user_id, chain, now))

    def replace_leaderboard(self, chain: str, rows: list[dict[str, Any]], ts: int) -> None:
        with self.lock:
            self._x("DELETE FROM leaderboard WHERE chain=?", (chain,))
            for r in rows:
                self._x("INSERT OR REPLACE INTO leaderboard(chain, wallet, label, pnl, wins, losses, trades, score, source, rank, flagged, updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (chain, r["wallet"], r["label"], r["pnl"], r["wins"], r["losses"], r["trades"], r["score"], r["source"],
                         r.get("rank"), r.get("flagged"), ts))

    def leaderboard(self, chain: str) -> list[dict[str, Any]]:
        return self._q("SELECT * FROM leaderboard WHERE chain=? ORDER BY flagged IS NOT NULL, rank", (chain,))

    # events
    def record_leader_event(self, ev: dict[str, Any]) -> bool:
        try:
            self._x("INSERT INTO leader_events(chain, signature, wallet, token, side, ts, token_amount, native_amount, symbol) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ev["chain"], ev["signature"], ev["wallet"], ev["token"], ev["side"], ev["timestamp"], ev["token_amount"], ev["native_amount"], ev.get("symbol")))
            return True
        except sqlite3.IntegrityError:
            return False

    def leader_events(self, chain: str | None, since_ts: int, side: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM leader_events WHERE ts>=?", [since_ts]
        if chain:
            sql += " AND chain=?"
            args.append(chain)
        if side:
            sql += " AND side=?"
            args.append(side)
        return self._q(sql + " ORDER BY ts", args)

    def leader_holding(self, chain: str, wallet: str, token: str) -> float:
        r = self._q("SELECT amount FROM leader_holdings WHERE chain=? AND wallet=? AND token=?", (chain, wallet, token))
        return float(r[0]["amount"]) if r else 0.0

    def set_leader_holding(self, chain: str, wallet: str, token: str, amount: float) -> None:
        self._x("INSERT OR REPLACE INTO leader_holdings(chain, wallet, token, amount) VALUES(?,?,?,?)", (chain, wallet, token, max(0.0, amount)))

    def holders_of(self, chain: str, token: str, wallets: list[str]) -> list[str]:
        if not wallets:
            return []
        q = ",".join("?" * len(wallets))
        return [r["wallet"] for r in self._q(f"SELECT wallet FROM leader_holdings WHERE chain=? AND token=? AND amount>0 AND wallet IN ({q})", (chain, token, *wallets))]

    def get_funder(self, chain: str, wallet: str) -> tuple[bool, str | None]:
        r = self._q("SELECT funder FROM funders WHERE chain=? AND wallet=?", (chain, wallet))
        return (True, r[0]["funder"]) if r else (False, None)

    def set_funder(self, chain: str, wallet: str, funder: str | None, ts: int) -> None:
        self._x("INSERT OR REPLACE INTO funders(chain, wallet, funder, checked_at) VALUES(?,?,?,?)", (chain, wallet, funder, ts))

    def ping(self) -> bool:
        try:
            self._q("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    # kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._q("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO kv(k, v) VALUES(?,?)", (key, json.dumps(value)))
