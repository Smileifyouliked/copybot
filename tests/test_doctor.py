"""Retrospective detection of a second writer.

The claim being tested is a hard bound, not a heuristic: one process polling
every N seconds cannot write more than 3600/N heartbeats in an hour. An hour
above that ceiling is proof another process was writing the same file.

These tests pin both directions -- it must fire on a real second writer, and it
must not fire on a single bot under any of the conditions that make a real bot's
heartbeat rate irregular.
"""
import dataclasses

import pytest

from copybot.db import Database
from copybot.doctor import diagnose, render

HOUR = 3600
T0 = 1_787_000_000 // HOUR * HOUR   # aligned to an hour boundary


@pytest.fixture
def cfg15(cfg):
    return dataclasses.replace(cfg, poll_interval_seconds=15)


def beats(db, start, hours, per_hour):
    """Write `per_hour` heartbeats in each of `hours` consecutive hours."""
    spacing = HOUR / per_hour
    for h in range(hours):
        for i in range(per_hour):
            db.conn.execute(
                "INSERT INTO heartbeat (ts, loop_ok, note) VALUES (?,1,'')",
                (int(start + h * HOUR + i * spacing),))
    db.conn.commit()


# --- it fires on a real second writer --------------------------------------

def test_two_writers_are_detected_and_dated(tmp_path, cfg15):
    """41 hours of two bots is 41 hours at roughly double the ceiling."""
    db = Database(tmp_path / "two.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=5, per_hour=240)          # one bot
    beats(db, T0 + 5 * HOUR, hours=41, per_hour=480)   # two bots
    beats(db, T0 + 46 * HOUR, hours=5, per_hour=240)   # one bot again

    report = diagnose(db, cfg15)
    assert report.had_two_writers
    assert len(report.windows) == 1, "the overlap is one contiguous window"
    w = report.windows[0]
    assert w.hours == pytest.approx(41.0)
    assert w.start_ts == T0 + 5 * HOUR
    assert w.peak_multiple == pytest.approx(2.0, abs=0.05)
    db.close()


def test_the_window_is_reported_with_real_timestamps(tmp_path, cfg15):
    db = Database(tmp_path / "dates.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=2, per_hour=480)
    text = render(diagnose(db, cfg15))
    assert "TWO WRITERS CONFIRMED" in text
    assert "UTC" in text
    assert "2.0h" in text
    db.close()


def test_separate_overlaps_are_reported_separately(tmp_path, cfg15):
    db = Database(tmp_path / "gaps.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=2, per_hour=480)
    beats(db, T0 + 2 * HOUR, hours=3, per_hour=240)
    beats(db, T0 + 5 * HOUR, hours=1, per_hour=480)
    report = diagnose(db, cfg15)
    assert len(report.windows) == 2
    assert report.contaminated_hours == pytest.approx(3.0)
    db.close()


# --- it does not fire on one bot -------------------------------------------

def test_a_single_bot_at_full_rate_does_not_trip_it(tmp_path, cfg15):
    db = Database(tmp_path / "one.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=48, per_hour=240)
    report = diagnose(db, cfg15)
    assert not report.had_two_writers
    assert "no evidence of a second writer" in render(report).lower()
    db.close()


def test_timer_jitter_does_not_manufacture_a_second_writer(tmp_path, cfg15):
    """A poll that finishes a hair early can squeeze an extra beat into an
    hour. That must not read as a second process."""
    db = Database(tmp_path / "jitter.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=10, per_hour=252)   # 5% over the ceiling
    assert not diagnose(db, cfg15).had_two_writers
    db.close()


def test_backoff_only_makes_the_test_quieter(tmp_path, cfg15):
    """During API trouble one bot polls far more slowly. The test must stay
    silent -- it under-reports by design and never invents a writer."""
    db = Database(tmp_path / "backoff.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=12, per_hour=12)
    assert not diagnose(db, cfg15).had_two_writers
    db.close()


def test_a_restart_within_the_hour_does_not_trip_it(tmp_path, cfg15):
    """Stopping and starting a bot writes no extra beats, it just interrupts
    them. Only genuine overlap exceeds the ceiling."""
    db = Database(tmp_path / "restart.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=1, per_hour=120)
    beats(db, T0 + HOUR, hours=1, per_hour=100)
    assert not diagnose(db, cfg15).had_two_writers
    db.close()


# --- what a race would actually damage -------------------------------------

def test_a_token_over_its_cap_is_reported(tmp_path, cfg15):
    """Two writers both reading 'spent so far' and both buying is the real
    failure mode. The ledger still reconciles, so only this check finds it."""
    db = Database(tmp_path / "over.sqlite3", cfg15.starting_capital_usd)
    with db.tx() as conn:
        for i in range(3):
            db.insert_execution(
                conn, position_id=None, trade_key=f"k{i}", token_id="TOK1",
                condition_id="0xc", question="Will X happen?", side="BUY",
                ts=T0 + i, shares=20.0, avg_fill=0.10, gross_usd=2.0,
                fee_usd=0.0, net_usd=-2.0, levels_consumed=1)
    report = diagnose(db, cfg15)
    assert len(report.budget_breaches) == 1
    assert report.budget_breaches[0]["spent"] == pytest.approx(6.0)
    assert "OVER the per-token cap" in render(report)
    db.close()


def test_a_token_inside_its_cap_is_not_reported(tmp_path, cfg15):
    db = Database(tmp_path / "under.sqlite3", cfg15.starting_capital_usd)
    with db.tx() as conn:
        db.insert_execution(
            conn, position_id=None, trade_key="k", token_id="TOK1",
            condition_id="0xc", question="Q", side="BUY", ts=T0,
            shares=20.0, avg_fill=0.10, gross_usd=2.0, fee_usd=0.0,
            net_usd=-2.0, levels_consumed=1)
    assert diagnose(db, cfg15).budget_breaches == []
    db.close()


def test_the_verdict_separates_rates_from_per_fill_numbers(tmp_path, cfg15):
    """Two writers with no damage still contaminates aggregate RATES while
    leaving each fill's own entry price intact. The verdict must say which is
    which rather than a blanket pass or fail."""
    db = Database(tmp_path / "verdict.sqlite3", cfg15.starting_capital_usd)
    beats(db, T0, hours=3, per_hour=480)
    text = render(diagnose(db, cfg15))
    assert "Aggregate RATES" in text
    assert "PER-FILL entry quality is still each fill's own number" in text
    db.close()
