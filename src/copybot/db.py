"""SQLite persistence.

Design rule: cash is never stored as a mutable number. It is derived from the
executions ledger (`copied_trades`, signed `net_usd`) so that restarting the
bot cannot drift from what actually happened. In-memory state that isn't in
here does not exist.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import ExitPath, MarkSource, Side, SkipReason, vwap

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every fill of his we have ever seen. Doubles as (a) the dedup ledger and
-- (b) the source for his volume-weighted average entry per token, since he
-- scales into a token across many fills but we only copy the first.
CREATE TABLE IF NOT EXISTS processed_trade_ids (
    trade_key    TEXT PRIMARY KEY,
    tx_hash      TEXT,
    token_id     TEXT NOT NULL,
    condition_id TEXT,
    side         TEXT NOT NULL,
    price        REAL NOT NULL,
    shares       REAL NOT NULL,
    usd_size     REAL NOT NULL,
    traded_ts    INTEGER NOT NULL,
    seen_ts      INTEGER NOT NULL,
    action       TEXT NOT NULL,          -- copied | skipped | mirrored | ignored
    title        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ptid_token ON processed_trade_ids(token_id, side, traded_ts);
CREATE INDEX IF NOT EXISTS idx_ptid_traded ON processed_trade_ids(traded_ts);

CREATE TABLE IF NOT EXISTS positions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id             TEXT NOT NULL,
    condition_id         TEXT NOT NULL,
    question             TEXT,
    outcome              TEXT,
    outcome_index        INTEGER,
    status               TEXT NOT NULL,   -- open | closed
    opened_ts            INTEGER NOT NULL,
    closed_ts            INTEGER,
    shares               REAL NOT NULL,   -- remaining
    shares_opened        REAL NOT NULL,
    cost_basis_usd       REAL NOT NULL,   -- remaining, fee-inclusive
    cost_basis_opened    REAL NOT NULL,
    our_avg_fill         REAL NOT NULL,
    entry_fee_usd        REAL NOT NULL DEFAULT 0,
    entry_band           TEXT,
    -- his side of the trade
    his_first_price      REAL,
    his_first_ts         INTEGER,
    his_vwap_entry       REAL,
    his_fill_count       INTEGER DEFAULT 0,
    his_total_shares     REAL DEFAULT 0,
    his_total_usd        REAL DEFAULT 0,
    -- comparison fields
    size_ratio           REAL,
    slippage_vs_his_entry      REAL,
    slippage_vs_his_entry_pct  REAL,
    slippage_vs_his_vwap       REAL,
    slippage_vs_his_vwap_pct   REAL,
    -- marking and CLV
    last_mark_price      REAL,
    last_mark_ts         INTEGER,
    last_mark_source     TEXT,
    closing_line_price   REAL,
    clv_abs              REAL,
    clv_pct              REAL,
    -- exit
    exit_path            TEXT,
    realised_pnl_usd     REAL NOT NULL DEFAULT 0,
    proceeds_usd         REAL NOT NULL DEFAULT 0,
    exit_fee_usd         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_pos_token ON positions(token_id, status);
CREATE INDEX IF NOT EXISTS idx_pos_condition ON positions(condition_id, status);

-- Our executions. net_usd is signed: negative = cash out, positive = cash in.
-- Cash is SUM(net_usd) + starting capital. Single source of truth.
CREATE TABLE IF NOT EXISTS copied_trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id          INTEGER REFERENCES positions(id),
    trade_key            TEXT,
    token_id             TEXT NOT NULL,
    condition_id         TEXT,
    question             TEXT,
    side                 TEXT NOT NULL,   -- BUY | SELL | SETTLE
    ts                   INTEGER NOT NULL,
    shares               REAL NOT NULL,
    avg_fill             REAL NOT NULL,
    gross_usd            REAL NOT NULL,
    fee_usd              REAL NOT NULL DEFAULT 0,
    net_usd              REAL NOT NULL,
    levels_consumed      INTEGER DEFAULT 0,
    fee_rate_used        REAL,
    fee_rate_was_fallback INTEGER DEFAULT 0,
    entry_band           TEXT,
    -- his side, for slippage measurement
    his_price            REAL,
    his_ts               INTEGER,
    his_vwap_at_copy     REAL,
    latency_seconds      INTEGER,
    size_ratio           REAL,
    slippage_vs_his_entry     REAL,
    slippage_vs_his_entry_pct REAL,
    realised_pnl_usd     REAL,
    note                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_ct_ts ON copied_trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_ct_position ON copied_trades(position_id);

CREATE TABLE IF NOT EXISTS skipped_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key     TEXT,
    token_id      TEXT,
    condition_id  TEXT,
    question      TEXT,
    side          TEXT,
    his_price     REAL,
    his_ts        INTEGER,
    seen_ts       INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    detail        TEXT,
    best_price    REAL,
    would_be_fill REAL
);
CREATE INDEX IF NOT EXISTS idx_skip_ts ON skipped_trades(seen_ts DESC);
CREATE INDEX IF NOT EXISTS idx_skip_reason ON skipped_trades(reason);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts                  INTEGER PRIMARY KEY,
    cash_usd            REAL NOT NULL,
    positions_value_usd REAL NOT NULL,
    total_equity_usd    REAL NOT NULL,
    open_positions      INTEGER NOT NULL,
    realised_pnl_usd    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    loop_ok      INTEGER NOT NULL,
    trades_seen  INTEGER DEFAULT 0,
    copies_made  INTEGER DEFAULT 0,
    skips        INTEGER DEFAULT 0,
    note         TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_hb_ts ON heartbeat(ts DESC);
"""


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str | Path, starting_capital_usd: float):
        self.path = Path(path)
        self.starting_capital_usd = float(starting_capital_usd)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- plumbing ----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit. A copy must never half-land."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    # -- dedup / his fill history -----------------------------------------
    def has_processed(self, trade_key: str) -> bool:
        return self._one(
            "SELECT 1 FROM processed_trade_ids WHERE trade_key = ?", (trade_key,)
        ) is not None

    def processed_keys(self) -> set[str]:
        return {r["trade_key"] for r in self._all("SELECT trade_key FROM processed_trade_ids")}

    def record_processed(self, trade, action: str, conn: sqlite3.Connection | None = None) -> bool:
        """Idempotent. Returns True if this was a new trade key."""
        c = conn or self._conn
        cur = c.execute(
            """INSERT OR IGNORE INTO processed_trade_ids
               (trade_key, tx_hash, token_id, condition_id, side, price, shares,
                usd_size, traded_ts, seen_ts, action, title)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.trade_key, trade.tx_hash, trade.token_id, trade.condition_id,
                trade.side.value, trade.price, trade.shares, trade.usd_size,
                trade.traded_ts, now_ts(), action, trade.title,
            ),
        )
        return cur.rowcount > 0

    def his_fills(self, token_id: str, side: str = Side.BUY.value,
                  up_to_ts: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT price, shares, usd_size, traded_ts FROM processed_trade_ids "
               "WHERE token_id = ? AND side = ?")
        params: list[Any] = [token_id, side]
        if up_to_ts is not None:
            sql += " AND traded_ts <= ?"
            params.append(up_to_ts)
        return self._all(sql + " ORDER BY traded_ts ASC", params)

    def his_vwap_entry(self, token_id: str, up_to_ts: int | None = None) -> tuple[float | None, int, float, float]:
        """His volume-weighted average BUY price for a token, plus fill count,
        total shares and total usd. He scales in; we only take his first fill,
        so this gap is a first-class measurement."""
        rows = self.his_fills(token_id, Side.BUY.value, up_to_ts)
        if not rows:
            return None, 0, 0.0, 0.0
        total_shares = sum(r["shares"] for r in rows)
        total_usd = sum(r["price"] * r["shares"] for r in rows)
        v = vwap((r["price"], r["shares"]) for r in rows)
        return v, len(rows), total_shares, total_usd

    def his_open_shares(self, token_id: str) -> float:
        """Net shares he currently holds of a token, from the fills we've seen.
        Used to work out what fraction of his position a sell represents."""
        row = self._one(
            """SELECT
                 COALESCE(SUM(CASE WHEN side='BUY'  THEN shares ELSE 0 END), 0) AS bought,
                 COALESCE(SUM(CASE WHEN side='SELL' THEN shares ELSE 0 END), 0) AS sold
               FROM processed_trade_ids WHERE token_id = ?""",
            (token_id,),
        )
        return max(0.0, (row["bought"] or 0.0) - (row["sold"] or 0.0)) if row else 0.0

    # -- cash --------------------------------------------------------------
    def cash(self) -> float:
        row = self._one("SELECT COALESCE(SUM(net_usd), 0) AS s FROM copied_trades")
        return self.starting_capital_usd + (row["s"] if row else 0.0)

    def realised_pnl(self) -> float:
        row = self._one("SELECT COALESCE(SUM(realised_pnl_usd), 0) AS s FROM positions")
        return row["s"] if row else 0.0

    def open_cost_basis(self) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_basis_usd), 0) AS s FROM positions WHERE status='open'"
        )
        return row["s"] if row else 0.0

    def reconcile(self, tolerance: float = 1e-6) -> tuple[bool, dict[str, float]]:
        """Invariant: cash + open cost basis - realised P&L == starting capital.

        (Cash already contains realised gains, so realised P&L is subtracted,
        not added -- adding it would double-count every closed position.)
        """
        cash = self.cash()
        basis = self.open_cost_basis()
        realised = self.realised_pnl()
        expected = self.starting_capital_usd
        actual = cash + basis - realised
        parts = {
            "cash": cash, "open_cost_basis": basis, "realised_pnl": realised,
            "expected": expected, "actual": actual, "diff": actual - expected,
        }
        return abs(actual - expected) <= tolerance, parts

    # -- positions ---------------------------------------------------------
    def open_positions(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM positions WHERE status='open' ORDER BY opened_ts DESC")

    def get_position(self, position_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM positions WHERE id = ?", (position_id,))

    def open_position_for_token(self, token_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM positions WHERE token_id = ? AND status='open' "
            "ORDER BY opened_ts ASC LIMIT 1",
            (token_id,),
        )

    def count_copies_for_token(self, token_id: str) -> int:
        """How many times we have bought this token, ever. Restart-safe:
        counts the ledger, not memory, and counts closed positions too so a
        round trip doesn't free up a second copy slot."""
        row = self._one(
            "SELECT COUNT(*) AS n FROM copied_trades WHERE token_id = ? AND side = 'BUY'",
            (token_id,),
        )
        return int(row["n"]) if row else 0

    def open_condition_ids(self) -> list[str]:
        return [r["condition_id"] for r in
                self._all("SELECT DISTINCT condition_id FROM positions WHERE status='open'")]

    def insert_position(self, conn: sqlite3.Connection, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        return int(cur.lastrowid)

    def update_position(self, conn: sqlite3.Connection, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE positions SET {sets} WHERE id = ?", (*fields.values(), position_id)
        )

    # -- executions --------------------------------------------------------
    def insert_execution(self, conn: sqlite3.Connection, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO copied_trades ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        return int(cur.lastrowid)

    def recent_executions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM copied_trades ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        )

    # -- skips -------------------------------------------------------------
    def record_skip(
        self,
        reason: SkipReason,
        *,
        trade=None,
        question: str = "",
        detail: str = "",
        best_price: float | None = None,
        would_be_fill: float | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        c = conn or self._conn
        c.execute(
            """INSERT INTO skipped_trades
               (trade_key, token_id, condition_id, question, side, his_price,
                his_ts, seen_ts, reason, detail, best_price, would_be_fill)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                getattr(trade, "trade_key", None),
                getattr(trade, "token_id", None),
                getattr(trade, "condition_id", None),
                question or getattr(trade, "title", "") or "",
                getattr(getattr(trade, "side", None), "value", None),
                getattr(trade, "price", None),
                getattr(trade, "traded_ts", None),
                now_ts(),
                reason.value,
                detail,
                best_price,
                would_be_fill,
            ),
        )

    def recent_skips(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM skipped_trades ORDER BY seen_ts DESC, id DESC LIMIT ?", (limit,)
        )

    def skip_counts(self) -> dict[str, int]:
        return {r["reason"]: r["n"] for r in
                self._all("SELECT reason, COUNT(*) AS n FROM skipped_trades GROUP BY reason")}

    # -- heartbeat / equity ------------------------------------------------
    def write_heartbeat(self, ok: bool, *, trades_seen: int = 0, copies_made: int = 0,
                        skips: int = 0, note: str = "", error: str = "") -> None:
        self._conn.execute(
            """INSERT INTO heartbeat (ts, loop_ok, trades_seen, copies_made, skips, note, error)
               VALUES (?,?,?,?,?,?,?)""",
            (now_ts(), 1 if ok else 0, trades_seen, copies_made, skips, note, error),
        )

    def last_heartbeat(self) -> sqlite3.Row | None:
        return self._one("SELECT * FROM heartbeat ORDER BY ts DESC, id DESC LIMIT 1")

    def prune_heartbeats(self, keep: int = 5000) -> None:
        """t3.small: keep the table from growing without bound at 15s polls."""
        self._conn.execute(
            "DELETE FROM heartbeat WHERE id NOT IN "
            "(SELECT id FROM heartbeat ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    def write_equity_snapshot(self, cash: float, positions_value: float,
                              open_positions: int, realised: float, ts: int | None = None) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO equity_snapshots
               (ts, cash_usd, positions_value_usd, total_equity_usd, open_positions, realised_pnl_usd)
               VALUES (?,?,?,?,?,?)""",
            (ts or now_ts(), cash, positions_value, cash + positions_value,
             open_positions, realised),
        )

    def last_equity_snapshot(self) -> sqlite3.Row | None:
        return self._one("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1")

    def equity_series(self, limit: int = 2000) -> list[sqlite3.Row]:
        rows = self._all(
            "SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return list(reversed(rows))
