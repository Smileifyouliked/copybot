"""The loop's own guarantees: one bot per database, and orders advanced every pass.

Both of these are here because of things that actually happened. A manually
started process ran alongside the systemd unit for 41 hours against the same
database file, and a resting order that is not polled inside its TTL is a fill
that can never be observed again -- the tape only answers about the window the
order was alive for.
"""
import dataclasses

import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.db import Database
from copybot.engine import AlreadyRunning, Engine, InstanceLock, register_run
from copybot.strategy import Strategy

T0 = 1_787_000_000


class StubClient(FakeClient):
    """A client whose activity feed we control."""

    def __init__(self, rows=(), **kw):
        super().__init__(**kw)
        self.rows = list(rows)
        self.activity_calls = 0

    def get_activity(self, wallet, limit=100):
        self.activity_calls += 1
        return list(self.rows)


def activity_row(price=0.20, tx="0xa", ts=T0, side="BUY"):
    return {"transactionHash": tx, "asset": "TOK1", "conditionId": "0xcond1",
            "side": side, "price": price, "size": 10.0, "usdcSize": price * 10,
            "timestamp": ts, "title": "Will X happen?", "outcome": "Yes",
            "outcomeIndex": 0}


# --- one bot per database --------------------------------------------------

def test_a_second_instance_refuses_to_start(tmp_path):
    path = tmp_path / "copybot.sqlite3"
    first = InstanceLock(path).acquire()
    try:
        with pytest.raises(AlreadyRunning, match="already holds"):
            InstanceLock(path).acquire()
    finally:
        first.release()


def test_the_lock_is_released_when_the_holder_stops(tmp_path):
    path = tmp_path / "copybot.sqlite3"
    InstanceLock(path).acquire().release()
    second = InstanceLock(path).acquire()   # must not raise
    second.release()


def test_a_stale_lock_file_locks_nobody_out(tmp_path):
    """The kernel owns the lock, not the file. A leftover file from a killed
    process must never be able to keep the bot from starting."""
    path = tmp_path / "copybot.sqlite3"
    (tmp_path / "copybot.sqlite3.lock").write_text("999999\n")
    lock = InstanceLock(path).acquire()
    lock.release()


# --- the run stamp ---------------------------------------------------------

def test_a_run_records_the_config_that_produced_it(tmp_path, cfg):
    """A config file that has since been edited cannot answer "what were the
    settings then", so the settings are snapshotted into the database."""
    import json
    db = Database(tmp_path / "run.sqlite3", cfg.starting_capital_usd)
    run_id = register_run(db, cfg, note="test")
    row = db.latest_run()
    assert row["run_id"] == run_id
    assert row["entry_mode"] == cfg.entry_mode
    snapshot = json.loads(row["config_json"])
    assert snapshot["max_entry_price"] == cfg.max_entry_price
    assert snapshot["stake_schedule"] == list(cfg.stake_schedule)
    db.close()


def test_positions_carry_the_run_that_opened_them(db, cfg):
    """A database spanning a market run and a limit run must still be able to
    answer either question separately."""
    market = dataclasses.replace(cfg, entry_mode="market", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    s = Strategy(market, db, FakeExecutor(books=books),
                 FakeClient(books=books, metas={"0xcond1": make_meta()}),
                 clock=lambda: T0, run_id="run-A")
    s.process_trades([make_trade(price=0.20, ts=T0)])
    assert db.open_position_for_token("TOK1")["run_id"] == "run-A"


# --- resting orders advance on every pass ----------------------------------

def _engine(db, cfg, books, rows, tape=(), now=T0):
    ex = FakeExecutor(books=books, tape=list(tape))
    cl = StubClient(rows=rows, books=books, metas={"0xcond1": make_meta()})
    strat = Strategy(cfg, db, ex, cl, clock=lambda: now, run_id="run-A")
    return Engine(cfg, db, cl, strat), strat, ex


def test_orders_are_advanced_on_a_pass_with_no_new_trades(db, cfg):
    """Most passes see nothing new from him. If those passes skip the tape, an
    order can expire without ever having been checked."""
    cfg = dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.22, 100_000)], bids=[])}
    rows = [activity_row()]
    eng, strat, ex = _engine(db, cfg, books, rows)

    eng.poll_once()
    assert len(db.open_resting()) == 1
    assert db.open_positions() == []

    # Second pass: same activity row (already processed), but the tape has moved.
    ex.tape.append({"price": 0.20, "size": 500.0, "timestamp": T0 + 30,
                    "side": "SELL", "proxyWallet": "0xsomeone", "asset": "TOK1"})
    strat.clock = lambda: T0 + 60
    eng.poll_once()
    assert db.open_position_for_token("TOK1") is not None, \
        "a pass with no new trades must still advance resting orders"


def test_market_mode_does_not_poll_orders(db, cfg):
    """Nothing rests in market mode, so the tape fetch is pure cost."""
    cfg = dataclasses.replace(cfg, entry_mode="market", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    eng, strat, _ = _engine(db, cfg, books, [activity_row()])
    calls = []
    strat.poll_resting_orders = lambda: calls.append(1) or {}
    eng.poll_once()
    assert calls == []


def test_the_heartbeat_reports_resting_orders(db, cfg):
    cfg = dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.22, 100_000)], bids=[])}
    eng, _, _ = _engine(db, cfg, books, [activity_row()])
    eng.poll_once()
    beat = db.last_heartbeat()
    assert beat["loop_ok"] == 1
    assert "rested=1" in beat["note"]
    assert "orders=" in beat["note"], "the order book state belongs in the heartbeat"
