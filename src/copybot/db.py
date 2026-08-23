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

-- One row per experiment. Runs are compared against each other, so each one
-- records the config that produced it and the code that ran it; a number with
-- no idea which rules generated it is not evidence.
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_ts  INTEGER NOT NULL,
    entry_mode  TEXT,
    git_commit  TEXT,
    config_json TEXT NOT NULL,
    note        TEXT
);

-- Limit orders waiting on the book. Persisted so a restart resumes them
-- instead of forgetting money is committed.
CREATE TABLE IF NOT EXISTS resting_orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT,
    trade_key          TEXT UNIQUE,
    token_id           TEXT NOT NULL,
    condition_id       TEXT,
    question           TEXT,
    limit_price        REAL NOT NULL,
    his_price          REAL NOT NULL,
    usd_budget         REAL NOT NULL,
    target_shares      REAL NOT NULL,
    queue_ahead_shares REAL NOT NULL,
    placed_ts          INTEGER NOT NULL,
    expires_ts         INTEGER NOT NULL,
    status             TEXT NOT NULL,   -- resting | filled | partial | expired
    filled_shares      REAL NOT NULL DEFAULT 0,
    filled_usd         REAL NOT NULL DEFAULT 0,
    fee_usd            REAL NOT NULL DEFAULT 0,
    consumed_shares    REAL NOT NULL DEFAULT 0,
    prints_observed    INTEGER NOT NULL DEFAULT 0,
    alt_filled_shares  REAL NOT NULL DEFAULT 0,
    alt_consumed_shares REAL NOT NULL DEFAULT 0,
    alt_prints_observed INTEGER NOT NULL DEFAULT 0,
    stake_variant_usd  REAL,
    settled_ts         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_resting_status ON resting_orders(status, expires_ts);
CREATE INDEX IF NOT EXISTS idx_resting_token ON resting_orders(token_id);

CREATE TABLE IF NOT EXISTS positions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT,
    stake_variant_usd    REAL,
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
    size_ratio                 REAL,   -- our usd / his FIRST fill usd
    his_position_usd_at_copy   REAL,   -- his total position when we copied
    his_position_usd_total     REAL,   -- his total position now (he scales in)
    size_ratio_vs_total        REAL,   -- our usd / his FULL position
    slippage_vs_his_entry      REAL,
    slippage_vs_his_entry_pct  REAL,
    slippage_vs_his_vwap       REAL,
    slippage_vs_his_vwap_pct   REAL,
    -- marking and CLV. CLV is the primary metric, so the capture is
    -- instrumented: a wide spread makes a mid-based CLV meaningless, and a
    -- stale capture makes it a different number than it claims to be.
    last_mark_price      REAL,
    last_mark_bid        REAL,
    last_mark_ask        REAL,
    last_mark_spread     REAL,
    last_mark_ts         INTEGER,
    last_mark_source     TEXT,
    closing_line_price   REAL,
    closing_line_bid     REAL,
    closing_line_ask     REAL,
    closing_line_spread  REAL,
    closing_line_ts      INTEGER,
    closing_line_age_seconds INTEGER,
    closing_line_captured    INTEGER NOT NULL DEFAULT 0,
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

-- Every size simulated against a signal's book snapshot, fills and skips alike.
-- Nothing here touches cash or positions; it is a capacity curve, collected for
-- free because it reuses the snapshot the real decision already fetched.
CREATE TABLE IF NOT EXISTS shadow_fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key         TEXT,
    token_id          TEXT NOT NULL,
    condition_id      TEXT,
    question          TEXT,
    seen_ts           INTEGER NOT NULL,
    his_price         REAL,
    his_ts            INTEGER,
    his_usd_size      REAL,
    book_timestamp_ms INTEGER,
    book_best_ask     REAL,
    book_ask_levels   INTEGER,
    outcome           TEXT,             -- what the real decision did
    rung_label        TEXT NOT NULL,    -- '$1' | '$3' | '$10' | 'his_fill' | 'his_position'
    rung_usd          REAL NOT NULL,
    filled            INTEGER NOT NULL,
    shares            REAL,
    vwap              REAL,
    depth_cost_pct    REAL,
    levels_consumed   INTEGER,
    cleared_max_fill  INTEGER,
    below_min_order_size INTEGER,
    fee_usd           REAL,
    skip_reason       TEXT,
    unmeasurable_reason TEXT     -- why depth_cost_pct is NULL; never dropped
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_fills(seen_ts DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_rung ON shadow_fills(rung_label);
CREATE INDEX IF NOT EXISTS idx_shadow_trade ON shadow_fills(trade_key);

-- Every mark, kept as its own row rather than overwriting one field. A single
-- overwritten mark makes CLV all-or-nothing on one capture landing in the window
-- before the book empties; a full path gives CLV at fixed horizons too, which
-- are always capturable because the book is still live at those points.
CREATE TABLE IF NOT EXISTS marks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    token_id    TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    bid         REAL,
    ask         REAL,
    mid         REAL,
    spread      REAL,
    source      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marks_pos ON marks(position_id, ts);
CREATE INDEX IF NOT EXISTS idx_marks_token ON marks(token_id, ts);

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
        self._migrate()
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def _migrate(self) -> None:
        """Additive column migrations. SQLite ALTER TABLE ADD COLUMN is cheap
        and a missing column on an existing database must not be a crash."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(positions)")}
        for col, decl in (
            ("his_position_usd_at_copy", "REAL"),
            ("his_position_usd_total", "REAL"),
            ("size_ratio_vs_total", "REAL"),
            ("last_mark_bid", "REAL"),
            ("last_mark_ask", "REAL"),
            ("last_mark_spread", "REAL"),
            ("closing_line_bid", "REAL"),
            ("closing_line_ask", "REAL"),
            ("closing_line_spread", "REAL"),
            ("closing_line_ts", "INTEGER"),
            ("closing_line_age_seconds", "INTEGER"),
            ("closing_line_captured", "INTEGER NOT NULL DEFAULT 0"),
            ("run_id", "TEXT"),
            ("stake_variant_usd", "REAL"),
        ):
            if col not in have:
                self._conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {decl}")
        shadow = {r["name"] for r in self._conn.execute("PRAGMA table_info(shadow_fills)")}
        if "unmeasurable_reason" not in shadow:
            self._conn.execute("ALTER TABLE shadow_fills ADD COLUMN unmeasurable_reason TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_marks_token ON marks(token_id, ts)")

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

    def resting_exposure(self, token_id: str) -> float:
        """Dollars sitting on the book for this token, still unfilled.

        A resting order is money committed even though no cash has moved: if it
        fills, it fills. Counting only executed fills against the per-token cap
        would let four of his fills place four orders against a three-order
        budget, and the cap would only be breached later, on the tape, where
        nothing checks it.
        """
        row = self._one(
            "SELECT COALESCE(SUM(MAX(usd_budget - filled_usd, 0)), 0) AS s "
            "FROM resting_orders WHERE token_id = ? "
            "AND status IN ('resting', 'partial')",
            (token_id,),
        )
        return float(row["s"]) if row else 0.0

    def committed_on_token(self, token_id: str) -> float:
        """Everything this token has claimed: cash spent plus resting exposure."""
        return self.spent_on_token(token_id) + self.resting_exposure(token_id)

    def count_commitments_for_token(self, token_id: str) -> int:
        """How many of his fills we are currently acting on for this token.

        Distinct by trade key, so a partially filled resting order -- which has
        both an execution and an open order -- claims one slot, not two. An
        order that expired without filling claims none: no money moved and no
        position exists, so the slot is genuinely free again.
        """
        row = self._one(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT trade_key FROM copied_trades "
            "   WHERE token_id = ? AND side = 'BUY' AND trade_key IS NOT NULL"
            "  UNION"
            "  SELECT trade_key FROM resting_orders "
            "   WHERE token_id = ? AND status IN ('resting', 'partial')"
            ")",
            (token_id, token_id),
        )
        return int(row["n"]) if row else 0

    def assigned_budget(self, token_id: str) -> float | None:
        """The per-token budget already assigned to this token, if any.

        The budget is chosen once, on the first fill we act on, and then reused
        for every later tranche. Recomputing it per fill would let a cheaper
        later price hand the token a smaller budget than it has already spent,
        which is exactly the kind of drift the hard cap exists to prevent.
        """
        row = self._one(
            "SELECT stake_variant_usd AS v FROM positions "
            "WHERE token_id = ? AND stake_variant_usd IS NOT NULL "
            "ORDER BY opened_ts ASC LIMIT 1",
            (token_id,),
        )
        if row and row["v"]:
            return float(row["v"])
        row = self._one(
            "SELECT stake_variant_usd AS v FROM resting_orders "
            "WHERE token_id = ? AND stake_variant_usd IS NOT NULL "
            "ORDER BY placed_ts ASC LIMIT 1",
            (token_id,),
        )
        return float(row["v"]) if row and row["v"] else None

    def spent_on_token(self, token_id: str) -> float:
        """Cash already committed to this token, fees included.

        The per-token budget is a hard cap, so this is measured from the
        executions ledger rather than tracked in memory -- a restart must not
        be able to forget that a token is already funded.
        """
        row = self._one(
            "SELECT COALESCE(SUM(-net_usd), 0) AS s FROM copied_trades "
            "WHERE token_id = ? AND side = 'BUY'",
            (token_id,),
        )
        return row["s"] if row else 0.0

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

    # -- runs --------------------------------------------------------------
    def start_run(self, run_id: str, cfg_json: str, entry_mode: str,
                  git_commit: str, note: str = "") -> str:
        self._conn.execute(
            """INSERT OR IGNORE INTO runs
               (run_id, started_ts, entry_mode, git_commit, config_json, note)
               VALUES (?,?,?,?,?,?)""",
            (run_id, now_ts(), entry_mode, git_commit, cfg_json, note),
        )
        return run_id

    def runs(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM runs ORDER BY started_ts")

    def latest_run(self) -> sqlite3.Row | None:
        return self._one("SELECT * FROM runs ORDER BY started_ts DESC LIMIT 1")

    # -- resting orders ----------------------------------------------------
    def upsert_resting(self, conn: sqlite3.Connection, order, *, run_id: str,
                       status: str, stake_variant: float | None = None) -> None:
        conn.execute(
            """INSERT INTO resting_orders
               (run_id, trade_key, token_id, condition_id, question, limit_price,
                his_price, usd_budget, target_shares, queue_ahead_shares,
                placed_ts, expires_ts, status, filled_shares, filled_usd, fee_usd,
                consumed_shares, prints_observed, alt_filled_shares,
                alt_consumed_shares, alt_prints_observed, stake_variant_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_key) DO UPDATE SET
                 status=excluded.status,
                 filled_shares=excluded.filled_shares,
                 filled_usd=excluded.filled_usd,
                 fee_usd=excluded.fee_usd,
                 consumed_shares=excluded.consumed_shares,
                 prints_observed=excluded.prints_observed,
                 alt_filled_shares=excluded.alt_filled_shares,
                 alt_consumed_shares=excluded.alt_consumed_shares,
                 alt_prints_observed=excluded.alt_prints_observed""",
            (run_id, order.his_trade_key, order.token_id, order.condition_id,
             order.question, order.limit_price, order.his_price, order.usd_budget,
             order.target_shares, order.queue_ahead_shares, order.placed_ts,
             order.expires_ts, status, order.filled_shares, order.filled_usd,
             order.fee_usd, order.consumed_shares, order.prints_observed,
             order.alt_filled_shares, order.alt_consumed_shares,
             order.alt_prints_observed, stake_variant),
        )

    def open_resting(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM resting_orders WHERE status='resting' "
                         "ORDER BY placed_ts")

    def close_resting(self, conn: sqlite3.Connection, trade_key: str,
                      status: str) -> None:
        conn.execute("UPDATE resting_orders SET status=?, settled_ts=? "
                     "WHERE trade_key=?", (status, now_ts(), trade_key))

    def fill_rate_stats(self, run_id: str | None = None) -> dict:
        """Of the orders we rested, what fraction filled at his price?

        The number the limit-order experiment turns on. Reported under both
        `side` conventions because which label means "seller" is unresolved.
        """
        where = "WHERE status != 'resting'"
        params: list = []
        if run_id:
            where += " AND run_id = ?"
            params.append(run_id)
        row = self._one(
            f"""SELECT COUNT(*) AS n,
                       SUM(filled_shares > 0) AS any_fill,
                       SUM(filled_shares >= target_shares - 1e-9) AS full_fill,
                       COALESCE(SUM(filled_shares), 0) AS shares,
                       COALESCE(SUM(target_shares), 0) AS target,
                       SUM(alt_filled_shares > 0) AS alt_any_fill,
                       COALESCE(SUM(alt_filled_shares), 0) AS alt_shares
                FROM resting_orders {where}""", params)
        n = row["n"] or 0
        return {
            "orders": n,
            "any_fill": row["any_fill"] or 0,
            "full_fill": row["full_fill"] or 0,
            "fill_rate": ((row["any_fill"] or 0) / n) if n else None,
            "full_fill_rate": ((row["full_fill"] or 0) / n) if n else None,
            "share_fill_rate": ((row["shares"] / row["target"]) if row["target"] else None),
            "alt_fill_rate": ((row["alt_any_fill"] or 0) / n) if n else None,
            "alt_share_fill_rate": ((row["alt_shares"] / row["target"])
                                    if row["target"] else None),
        }

    def vwap_ratio_stats(self, run_id: str | None = None,
                         breakeven: float = 1.395) -> dict:
        """our VWAP / his VWAP per token -- the headline entry-quality metric.

        Break-even is 1.395 (NOTES.md S10): above it we lose money on a
        confirmed edge, below it we make money. The share of positions above
        that line is reported alongside the average, because an average of 1.2
        made of half at 1.0 and half at 1.4 is a different business from every
        position sitting at 1.2.
        """
        where = "WHERE our_avg_fill > 0 AND his_vwap_entry > 0"
        params: list = []
        if run_id:
            where += " AND run_id = ?"
            params.append(run_id)
        ratios = sorted(
            r["ratio"] for r in self._all(
                f"SELECT our_avg_fill / his_vwap_entry AS ratio FROM positions {where}",
                params)
        )
        if not ratios:
            return {"n": 0, "median": None, "mean": None, "p90": None,
                    "best": None, "worst": None, "over_breakeven": 0,
                    "over_breakeven_rate": None, "breakeven": breakeven}
        n = len(ratios)
        over = sum(1 for r in ratios if r > breakeven)
        return {
            "n": n,
            "median": ratios[n // 2],
            "mean": sum(ratios) / n,
            "p90": ratios[min(n - 1, int(n * 0.9))],
            "best": ratios[0],
            "worst": ratios[-1],
            "over_breakeven": over,
            "over_breakeven_rate": over / n,
            "breakeven": breakeven,
        }

    def variant_breakdown(self, run_id: str | None = None,
                          breakeven: float = 1.395) -> list[dict]:
        """Fill rate and entry quality per stake, side by side.

        A resting order is punished by queue position, not by depth: a smaller
        order can fill far more often at the same price, which is the opposite
        of how crossing behaves. That makes stake a variable worth testing
        rather than a constant nobody questioned.

        Read this WITHIN a price band, not pooled. The exchange minimum is a
        share count, so a $1 order is unplaceable above 20c and the $1 arm
        therefore holds cheaper tokens than the $3 arm. That is physical, not
        fixable, and pooling the arms would compare stakes by comparing prices.
        """
        where, params = "", []
        if run_id:
            where, params = " AND run_id = ?", [run_id]

        orders = self._all(
            f"""SELECT stake_variant_usd AS v,
                       COUNT(*) AS orders,
                       SUM(filled_shares > 0) AS any_fill,
                       COALESCE(SUM(filled_shares), 0) AS shares,
                       COALESCE(SUM(target_shares), 0) AS target,
                       SUM(alt_filled_shares > 0) AS alt_any_fill
                FROM resting_orders
                WHERE status != 'resting' AND stake_variant_usd IS NOT NULL{where}
                GROUP BY v ORDER BY v""", params)

        ratios: dict[float, list[float]] = {}
        for row in self._all(
            f"""SELECT stake_variant_usd AS v, our_avg_fill / his_vwap_entry AS ratio
                FROM positions
                WHERE our_avg_fill > 0 AND his_vwap_entry > 0
                  AND stake_variant_usd IS NOT NULL{where}""", params):
            ratios.setdefault(row["v"], []).append(row["ratio"])

        out = []
        for v in sorted(set(list(ratios) + [r["v"] for r in orders])):
            o = next((r for r in orders if r["v"] == v), None)
            rs = sorted(ratios.get(v, []))
            n = len(rs)
            out.append({
                "stake": v,
                "orders": (o["orders"] if o else 0),
                "fill_rate": ((o["any_fill"] or 0) / o["orders"]) if o and o["orders"] else None,
                "alt_fill_rate": ((o["alt_any_fill"] or 0) / o["orders"])
                                 if o and o["orders"] else None,
                "share_fill_rate": ((o["shares"] / o["target"]) if o and o["target"] else None),
                "positions": n,
                "mean_ratio": (sum(rs) / n) if n else None,
                "median_ratio": rs[n // 2] if n else None,
                "over_breakeven": sum(1 for r in rs if r > breakeven),
            })
        return out

    # -- shadow ladder -----------------------------------------------------
    def record_shadow_ladder(self, conn, rungs, *, trade=None, book=None,
                             question: str = "", outcome: str = "",
                             his_usd_size: float | None = None) -> None:
        for rung in rungs:
            conn.execute(
                """INSERT INTO shadow_fills
                   (trade_key, token_id, condition_id, question, seen_ts,
                    his_price, his_ts, his_usd_size, book_timestamp_ms,
                    book_best_ask, book_ask_levels, outcome, rung_label, rung_usd,
                    filled, shares, vwap, depth_cost_pct, levels_consumed,
                    cleared_max_fill, below_min_order_size, fee_usd, skip_reason,
                    unmeasurable_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    getattr(trade, "trade_key", None),
                    getattr(trade, "token_id", None) or (book.token_id if book else ""),
                    getattr(trade, "condition_id", None),
                    question, now_ts(),
                    getattr(trade, "price", None),
                    getattr(trade, "traded_ts", None),
                    his_usd_size,
                    book.timestamp_ms if book else None,
                    book.best_ask if book else None,
                    len(book.asks) if book else None,
                    outcome, rung.label, rung.usd,
                    1 if rung.filled else 0, rung.shares, rung.vwap,
                    rung.depth_cost_pct, rung.levels_consumed,
                    1 if rung.cleared_max_fill else 0,
                    1 if rung.below_min_order_size else 0,
                    rung.fee_usd, rung.skip_reason, rung.unmeasurable_reason,
                ),
            )

    def shadow_ladder_summary(self) -> list[dict]:
        """Median depth cost and clear rate per rung -- the capacity curve."""
        """Capacity curve, computed on PAIRED signals only.

        Only signals where every rung produced a depth cost are included, so
        each rung's median is taken over exactly the same set of book snapshots.
        A median that silently spans different subsets is worse than no number.

        The representative size per rung is the **median**, not the mean:
        `his_fill` and `his_position` vary per signal and their means are pulled
        far above their medians by a few large fills. Reporting a mean size
        beside a median cost invites a false inversion.
        """
        paired = [r["trade_key"] for r in self._all(
            """SELECT trade_key FROM shadow_fills
               WHERE trade_key IS NOT NULL
               GROUP BY trade_key
               HAVING SUM(depth_cost_pct IS NULL) = 0""")]
        if not paired:
            return []
        marks = ",".join("?" * len(paired))
        rows = self._all(
            f"""SELECT rung_label, rung_usd, depth_cost_pct, filled,
                       cleared_max_fill, levels_consumed
                FROM shadow_fills WHERE trade_key IN ({marks})""", paired)

        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["rung_label"], []).append(row)

        def median(values: list[float]):
            if not values:
                return None
            ordered = sorted(values)
            return ordered[len(ordered) // 2]

        out = []
        for label, group in grouped.items():
            n = len(group)
            costs = sorted(r["depth_cost_pct"] for r in group)
            filled = [r for r in group if r["filled"]]
            out.append({
                "rung": label,
                "n": n,
                "paired_signals": len(paired),
                "median_usd": median([r["rung_usd"] for r in group]),
                "min_usd": min(r["rung_usd"] for r in group),
                "max_usd": max(r["rung_usd"] for r in group),
                "median_depth_cost_pct": median(costs),
                "p90_depth_cost_pct": costs[min(n - 1, int(n * 0.9))],
                "fill_rate": len(filled) / n,
                "clear_rate": sum(r["cleared_max_fill"] for r in group) / n,
                "mean_levels": (sum(r["levels_consumed"] or 0 for r in filled) / len(filled))
                               if filled else None,
            })
        return sorted(out, key=lambda r: r["median_usd"])

    def depth_cost_distribution(self, rung_label: str = "$3") -> list[float]:
        """Measured depth costs for one rung, on paired signals only."""
        return [r["depth_cost_pct"] for r in self._all(
            """SELECT depth_cost_pct FROM shadow_fills WHERE rung_label = ?
               AND depth_cost_pct IS NOT NULL
               AND trade_key IN (SELECT trade_key FROM shadow_fills
                                 WHERE trade_key IS NOT NULL GROUP BY trade_key
                                 HAVING SUM(depth_cost_pct IS NULL) = 0)
               ORDER BY depth_cost_pct""", (rung_label,))]

    def unmeasurable_ladder_counts(self) -> dict[str, int]:
        return {r["unmeasurable_reason"]: r["n"] for r in self._all(
            "SELECT unmeasurable_reason, COUNT(*) AS n FROM shadow_fills "
            "WHERE unmeasurable_reason IS NOT NULL GROUP BY unmeasurable_reason")}

    # -- marks -------------------------------------------------------------
    def record_mark(self, conn, position_id: int, token_id: str, ts: int,
                    bid, ask, mid, spread, source: str) -> None:
        conn.execute(
            """INSERT INTO marks (position_id, token_id, ts, bid, ask, mid, spread, source)
               VALUES (?,?,?,?,?,?,?,?)""",
            (position_id, token_id, ts, bid, ask, mid, spread, source),
        )

    def marks_for(self, position_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM marks WHERE position_id = ? ORDER BY ts ASC", (position_id,))

    def mark_nearest(self, position_id: int, target_ts: int,
                     tolerance_seconds: int, book_only: bool = True):
        """The mark closest to `target_ts` within tolerance.

        Book-derived only by default: a gamma fallback price for a resolved
        market is the outcome, not a market price, and would turn a horizon CLV
        into a restatement of the result.
        """
        sql = ("SELECT *, ABS(ts - ?) AS distance FROM marks "
               "WHERE position_id = ? AND ABS(ts - ?) <= ?")
        params = [target_ts, position_id, target_ts, tolerance_seconds]
        if book_only:
            sql += " AND source IN ('book_mid','book_bid')"
        return self._one(sql + " ORDER BY distance ASC LIMIT 1", params)

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
