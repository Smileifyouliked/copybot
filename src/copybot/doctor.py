"""Retrospective checks on a run's database.

Written to answer one question that could not be answered by looking at the
code: *did two bots ever write this file, and if so, what did it do to the
data?* A manually started process was once found running alongside the systemd
unit, and "I think it was fine" is not an answer you can deploy on.

The heartbeat table settles it without needing any record of what was running.
One process writes at most one heartbeat per poll interval, so it cannot
produce more than `3600 / poll_interval_seconds` heartbeats in an hour. That is
a hard ceiling, not a heuristic: an hour above it is proof that something else
was writing the same file. Backoff makes the rate LOWER during API trouble, so
the test only ever under-reports; it cannot invent a second writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import Database

# Timer jitter and boundary effects can push a bucket a little over the ceiling
# without a second process existing. 10% is far below the 2x a real second
# writer produces, and well above anything scheduling noise can manufacture.
JITTER = 1.10


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class Window:
    start_ts: int
    end_ts: int
    peak_multiple: float
    total_beats: int

    @property
    def hours(self) -> float:
        return (self.end_ts - self.start_ts) / 3600.0

    def describe(self) -> str:
        return (f"{_utc(self.start_ts)} -> {_utc(self.end_ts)}  "
                f"({self.hours:.1f}h, peak {self.peak_multiple:.1f}x the rate one "
                f"process can produce)")


@dataclass
class Report:
    poll_interval: int
    ceiling_per_hour: float
    windows: list[Window] = field(default_factory=list)
    budget_breaches: list[dict] = field(default_factory=list)
    duplicate_positions: list[dict] = field(default_factory=list)
    reconciles: bool = True
    reconcile_parts: dict = field(default_factory=dict)
    buys: int = 0
    positions: int = 0
    hours_covered: float = 0.0

    @property
    def had_two_writers(self) -> bool:
        return bool(self.windows)

    @property
    def contaminated_hours(self) -> float:
        return sum(w.hours for w in self.windows)


def concurrent_writer_windows(db: Database, poll_interval: int) -> tuple[list[Window], float]:
    """Hours in which more heartbeats were written than one process can write."""
    rows = db.conn.execute("SELECT ts FROM heartbeat ORDER BY ts").fetchall()
    if not rows:
        return [], 0.0
    ceiling = 3600.0 / max(1, poll_interval)

    buckets: dict[int, int] = {}
    for row in rows:
        buckets[row["ts"] // 3600] = buckets.get(row["ts"] // 3600, 0) + 1

    windows: list[Window] = []
    current: Window | None = None
    for hour in sorted(buckets):
        beats = buckets[hour]
        multiple = beats / ceiling
        over = multiple > JITTER
        if over and current is not None and hour * 3600 == current.end_ts:
            current.end_ts = (hour + 1) * 3600
            current.peak_multiple = max(current.peak_multiple, multiple)
            current.total_beats += beats
        elif over:
            if current is not None:
                windows.append(current)
            current = Window(hour * 3600, (hour + 1) * 3600, multiple, beats)
        elif current is not None:
            windows.append(current)
            current = None
    if current is not None:
        windows.append(current)

    span = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0
    return windows, span


def budget_breaches(db: Database, cap: float) -> list[dict]:
    """Tokens that took more money than the per-token cap allows.

    This is what a race between two writers would actually produce: both read
    "spent so far", both decide there is room, both buy. The ledger is still
    internally consistent -- no torn rows -- so reconciliation cannot catch it.
    """
    rows = db.conn.execute(
        """SELECT token_id, question, COUNT(*) AS fills, SUM(-net_usd) AS spent
           FROM copied_trades WHERE side='BUY'
           GROUP BY token_id HAVING spent > ? + 0.01
           ORDER BY spent DESC""", (cap,)).fetchall()
    return [dict(r) for r in rows]


def duplicate_positions(db: Database) -> list[dict]:
    """More than one OPEN position on the same token.

    Tranches are supposed to merge into one holding. Two of them means two
    writers opened the same token at the same moment, or the merge failed.
    """
    rows = db.conn.execute(
        """SELECT token_id, question, COUNT(*) AS n FROM positions
           WHERE status='open' GROUP BY token_id HAVING n > 1""").fetchall()
    return [dict(r) for r in rows]


def diagnose(db: Database, cfg) -> Report:
    windows, span = concurrent_writer_windows(db, cfg.poll_interval_seconds)
    ok, parts = db.reconcile()
    return Report(
        poll_interval=cfg.poll_interval_seconds,
        ceiling_per_hour=3600.0 / max(1, cfg.poll_interval_seconds),
        windows=windows,
        budget_breaches=budget_breaches(db, cfg.stake_per_copy_usd),
        duplicate_positions=duplicate_positions(db),
        reconciles=ok,
        reconcile_parts=parts,
        buys=db.conn.execute(
            "SELECT COUNT(*) AS n FROM copied_trades WHERE side='BUY'").fetchone()["n"],
        positions=db.conn.execute(
            "SELECT COUNT(*) AS n FROM positions").fetchone()["n"],
        hours_covered=span,
    )


def render(report: Report) -> str:
    out = ["RUN HEALTH CHECK", ""]
    out.append(f"  {report.buys} buy(s) across {report.positions} position(s), "
               f"{report.hours_covered:.1f}h of heartbeats")
    out.append("")

    out.append("── WAS THIS FILE EVER WRITTEN BY TWO BOTS AT ONCE ──")
    out.append(f"  One process polls every {report.poll_interval}s, so it cannot write more")
    out.append(f"  than {report.ceiling_per_hour:.0f} heartbeats in an hour. An hour above that is")
    out.append("  proof of a second writer, not a guess. (Backoff only lowers the")
    out.append("  rate, so this test under-reports; it cannot invent a second bot.)")
    if not report.windows:
        out.append("  -> no hour exceeded the ceiling. No evidence of a second writer.")
    else:
        out.append(f"  -> TWO WRITERS CONFIRMED across {report.contaminated_hours:.1f} hour(s):")
        for w in report.windows:
            out.append(f"     {w.describe()}")
    out.append("")

    out.append("── DID IT CORRUPT ANYTHING ──")
    out.append("  Two writers cannot tear a single row: SQLite serialises writes, so")
    out.append("  every execution row is one process's complete result. What a race")
    out.append("  CAN do is let both read 'spent so far', both see room, and both")
    out.append("  buy -- overspending a token while the ledger still reconciles.")
    if report.budget_breaches:
        out.append(f"  -> {len(report.budget_breaches)} token(s) OVER the per-token cap:")
        for row in report.budget_breaches[:10]:
            out.append(f"     ${row['spent']:.2f} in {row['fills']} fill(s): "
                       f"{(row['question'] or row['token_id'])[:52]}")
    else:
        out.append("  -> no token exceeded its cap. The dedup gate held.")

    if report.duplicate_positions:
        out.append(f"  -> {len(report.duplicate_positions)} token(s) with more than one open "
                   f"position (tranches should merge into one):")
        for row in report.duplicate_positions[:10]:
            out.append(f"     {row['n']}x {(row['question'] or row['token_id'])[:52]}")
    else:
        out.append("  -> no token has two open positions.")

    out.append(f"  -> ledger reconciles: {'yes' if report.reconciles else 'NO'}"
               + ("" if report.reconciles else f"  {report.reconcile_parts}"))
    out.append("")

    out.append("── VERDICT ──")
    if not report.windows:
        out.append("  Usable as a baseline. No second writer, so entry prices in this")
        out.append("  file are this bot's own decisions.")
    elif not report.budget_breaches and not report.duplicate_positions and report.reconciles:
        out.append("  Two bots wrote this file, but they left no damage the data can")
        out.append("  show: no cap breached, no token opened twice, ledger balances.")
        out.append("  Aggregate RATES (fills per day, capital deployed) span two")
        out.append("  processes and should not be read as one bot's behaviour.")
        out.append("  PER-FILL entry quality is still each fill's own number and is")
        out.append("  the thing worth comparing across runs.")
    else:
        out.append("  Two bots wrote this file AND the checks above found damage.")
        out.append("  Do not use this run as a baseline without excluding the")
        out.append("  affected tokens by hand.")
    return "\n".join(out)
