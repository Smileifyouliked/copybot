"""Archiving and run-to-run comparison.

The rule: a run is never deleted. The market-order run cost real observation
time and is the only baseline the limit-order run can be judged against.
"""
import dataclasses
import sqlite3
from datetime import datetime, timezone

import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.archive import archive, archive_path, compare, entry_quality
from copybot.db import Database
from copybot.strategy import Strategy

T0 = 1_787_000_000


def open_one(db, cfg, price=0.20, his_price=None, run_id="run-A", token="TOK1",
             min_size=1.0):
    """One taker copy, so the position has both VWAPs filled in.

    `min_size` defaults to 1 share here: these tests need a position at a fill
    price deliberately worse than his, and the real 5-share minimum -- which is
    sized off HIS price -- refuses exactly that. The floor is tested where it
    belongs, in test_budget.py.
    """
    books = {token: make_book(token_id=token, asks=[(price, 100_000)],
                              bids=[(price - 0.01, 100_000)], min_size=min_size)}
    s = Strategy(dataclasses.replace(cfg, entry_mode="market",
                                     stake_variants_usd=[3.00]),
                 db, FakeExecutor(books=books),
                 FakeClient(books=books, metas={"0xcond1": make_meta()}),
                 clock=lambda: T0, run_id=run_id)
    s.process_trades([make_trade(token_id=token, price=his_price or price, ts=T0)])
    return s


# --- archiving -------------------------------------------------------------

def test_archive_leaves_the_original_in_place(tmp_path, cfg):
    path = tmp_path / "copybot.sqlite3"
    db = Database(path, cfg.starting_capital_usd)
    open_one(db, cfg)
    before = db.open_positions()[0]["our_avg_fill"]
    db.close()

    dest = archive(path)
    assert path.exists(), "the original database must never be removed"
    assert dest.exists() and dest != path

    reopened = Database(path, cfg.starting_capital_usd)
    assert reopened.open_positions()[0]["our_avg_fill"] == pytest.approx(before)
    reopened.close()


def test_the_archive_is_a_complete_readable_copy(tmp_path, cfg):
    path = tmp_path / "copybot.sqlite3"
    db = Database(path, cfg.starting_capital_usd)
    open_one(db, cfg)
    expected = len(db.open_positions())
    db.close()

    dest = archive(path)
    copy = sqlite3.connect(dest)
    n = copy.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    copy.close()
    assert n == expected


def test_archiving_twice_in_one_second_refuses_rather_than_overwrites(tmp_path, cfg):
    path = tmp_path / "copybot.sqlite3"
    Database(path, cfg.starting_capital_usd).close()
    when = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
    archive(path, when)
    with pytest.raises(FileExistsError):
        archive(path, when)


def test_archiving_a_missing_database_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        archive(tmp_path / "nope.sqlite3")


def test_archive_name_carries_the_timestamp(tmp_path):
    when = datetime(2026, 8, 23, 12, 34, 56, tzinfo=timezone.utc)
    p = archive_path(tmp_path / "copybot.sqlite3", when)
    assert p.name == "copybot-20260823T123456Z.sqlite3"


# --- comparison ------------------------------------------------------------

def test_entry_quality_is_reported_per_run(tmp_path, cfg):
    """One database can span a market run and a limit run, and each must still
    be answerable on its own."""
    path = tmp_path / "two-runs.sqlite3"
    db = Database(path, cfg.starting_capital_usd)
    db.start_run("run-A", "{}", "market", "abc123")
    db.start_run("run-B", "{}", "limit", "abc123")
    open_one(db, cfg, price=0.20, his_price=0.18, run_id="run-A", token="TOK1")
    open_one(db, cfg, price=0.10, his_price=0.10, run_id="run-B", token="TOK9")

    a = entry_quality(db, "run-A")["vwap"]
    b = entry_quality(db, "run-B")["vwap"]
    assert a["n"] == 1 and b["n"] == 1
    assert a["mean"] > b["mean"], \
        "the run that paid above his price must show the worse ratio"
    db.close()


def test_over_breakeven_counts_the_positions_that_lose_money(tmp_path, cfg):
    """An average of 1.2 made of half at 1.0 and half at 1.4 is a different
    business from every position sitting at 1.2."""
    path = tmp_path / "ratios.sqlite3"
    db = Database(path, cfg.starting_capital_usd)
    open_one(db, cfg, price=0.30, his_price=0.10, token="TOK1")   # ratio 3.0
    open_one(db, cfg, price=0.10, his_price=0.10, token="TOK2")   # ratio 1.0
    stats = db.vwap_ratio_stats(breakeven=1.395)
    assert stats["n"] == 2
    assert stats["over_breakeven"] == 1
    assert stats["over_breakeven_rate"] == pytest.approx(0.5)
    db.close()


def test_compare_renders_two_databases_without_pnl(tmp_path, cfg):
    """P&L is deliberately absent: two runs see different markets, so the gap
    between them is mostly which coin flips landed."""
    a, b = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    for path, price in ((a, 0.30), (b, 0.11)):
        db = Database(path, cfg.starting_capital_usd)
        open_one(db, cfg, price=price, his_price=0.10)
        db.close()

    text = compare([a, b], cfg.starting_capital_usd, cfg.vwap_breakeven_ratio)
    assert "old" in text and "new" in text
    assert "1.395" in text
    assert "P&L is not compared" in text
    assert "$" not in text.split("break-even")[0], \
        "no dollar figures: the runs are not comparable on money"


# --- the kill condition on entry quality -----------------------------------

def test_the_ratio_kill_rule_waits_for_enough_bets(tmp_path, cfg):
    """One bad entry is noise. The rule does not fire until there are enough
    of them to mean something."""
    from copybot.analytics import stopping_rules
    db = Database(tmp_path / "kill.sqlite3", cfg.starting_capital_usd)
    open_one(db, cfg, price=0.30, his_price=0.10)   # ratio 3.0, way over the line
    rule = next(r for r in stopping_rules(db, cfg)["kill"]
                if r["name"].startswith("What we pay"))
    assert rule["status"] == "waiting", "one bet must not trip a kill condition"
    assert f"1 / {cfg.kill_vwap_min_fills}" in rule["progress"]
    db.close()


def test_the_ratio_kill_rule_fires_once_the_evidence_is_in(tmp_path, cfg):
    import dataclasses
    from copybot.analytics import stopping_rules
    cheap = dataclasses.replace(cfg, kill_vwap_min_fills=3)
    db = Database(tmp_path / "kill2.sqlite3", cheap.starting_capital_usd)
    for i in range(3):
        open_one(db, cheap, price=0.30, his_price=0.10, token=f"T{i}")
    rule = next(r for r in stopping_rules(db, cheap)["kill"]
                if r["name"].startswith("What we pay"))
    assert rule["status"] == "breach"
    assert rule["value"].startswith("3.000")
    db.close()


def test_entries_inside_the_line_do_not_trip_it(tmp_path, cfg):
    import dataclasses
    from copybot.analytics import stopping_rules
    cheap = dataclasses.replace(cfg, kill_vwap_min_fills=3)
    db = Database(tmp_path / "kill3.sqlite3", cheap.starting_capital_usd)
    for i in range(3):
        open_one(db, cheap, price=0.11, his_price=0.10, token=f"T{i}")
    rule = next(r for r in stopping_rules(db, cheap)["kill"]
                if r["name"].startswith("What we pay"))
    assert rule["status"] == "ok"
    db.close()
