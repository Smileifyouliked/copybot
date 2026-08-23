"""Archiving a run, and comparing two runs.

The rule this file exists to enforce: **a run is never deleted.** The market-
order run cost real observation time and is the only baseline the limit-order
run can be judged against. Starting fresh means copying the old database aside
and beginning a new one, never truncating tables in place.

Comparison is deliberately narrow. The two runs did not see the same markets at
the same times, so a P&L difference between them is mostly luck. What IS
comparable is entry quality against the wallet we copy -- our VWAP over his
VWAP, per token, on the same wallet's fills -- because that ratio is normalised
by his price and 1.395 is the level where the whole thing stops working.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import Database


def archive_path(db_path: str | Path, when: datetime | None = None) -> Path:
    when = when or datetime.now(timezone.utc)
    p = Path(db_path)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    return p.with_name(f"{p.stem}-{stamp}{p.suffix}")


def archive(db_path: str | Path, when: datetime | None = None) -> Path:
    """Copy the database aside and leave the original untouched.

    The copy is taken through SQLite's own backup API rather than `cp`, so a
    running bot mid-transaction cannot hand us a torn file. The original is
    never modified, never truncated and never removed -- starting a fresh run
    means moving the ORIGINAL out of the way by hand, or pointing db_path at a
    new file, both of which are the operator's call and not this function's.
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"no database at {src}")
    dest = archive_path(src, when)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; refusing to overwrite an archive")

    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def entry_quality(db: Database, run_id: str | None = None,
                  breakeven: float = 1.395) -> dict:
    """Everything comparable between two runs, in one row.

    Deliberately excludes P&L: two runs see different markets, so a P&L gap is
    mostly which coin flips landed. Entry quality is normalised by his own
    price on his own picks, which is why it survives the comparison.
    """
    ratio = db.vwap_ratio_stats(run_id, breakeven)
    fills = db.fill_rate_stats(run_id)
    return {"run_id": run_id or "(all)", "vwap": ratio, "fills": fills}


def _fmt(value, spec="{:.3f}", dash="  --  "):
    return dash if value is None else spec.format(value)


def compare(paths: list[str | Path], capital: float, breakeven: float) -> str:
    """Render two or more run databases side by side.

    Each path may be a database file; runs inside one file are compared
    separately when it holds more than one, because a single file can span a
    market-order run and a limit-order run.
    """
    rows: list[tuple[str, dict]] = []
    for path in paths:
        db = Database(path, capital)
        try:
            runs = db.runs()
            label_base = Path(path).stem
            if len(runs) <= 1:
                label = f"{label_base}"
                if runs:
                    label += f" [{runs[0]['entry_mode']}]"
                rows.append((label, entry_quality(db, breakeven=breakeven)))
            else:
                for run in runs:
                    rows.append((f"{label_base}/{run['run_id']} [{run['entry_mode']}]",
                                 entry_quality(db, run["run_id"], breakeven)))
        finally:
            db.close()

    out = ["ENTRY QUALITY BY RUN",
           "",
           "P&L is not compared: two runs see different markets, so the gap "
           "between them is mostly",
           "which coin flips landed. What compares is entry quality against the "
           "wallet we copy.",
           ""]
    header = (f"{'run':<40} {'copies':>7} {'our/his':>9} {'median':>8} "
              f"{'over':>6} {'orders':>7} {'fill%':>7} {'alt%':>7}")
    out.append(header)
    out.append("-" * len(header))
    for label, q in rows:
        v, f = q["vwap"], q["fills"]
        out.append(
            f"{label[:40]:<40} {v.get('n') or 0:>7} "
            f"{_fmt(v.get('mean')):>9} {_fmt(v.get('median')):>8} "
            f"{_fmt(v.get('over_breakeven_rate'), '{:.0%}'):>6} "
            f"{f.get('orders') or 0:>7} "
            f"{_fmt(f.get('fill_rate'), '{:.1%}'):>7} "
            f"{_fmt(f.get('alt_fill_rate'), '{:.1%}'):>7}"
        )
    out += [
        "",
        f"break-even is our VWAP / his VWAP = {breakeven:.3f}. Above it, his "
        f"+41.3% net edge on the",
        "sub-50c slice is spent entirely on our entry and there is nothing left.",
        "",
        "fill%  is under the configured `side` convention; alt%  is the same "
        "orders read the",
        "opposite way. If those two diverge, neither number can be trusted "
        "until side-check settles it.",
    ]
    return "\n".join(out)
