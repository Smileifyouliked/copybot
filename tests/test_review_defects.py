"""Regression tests for the defects a code review of run 2 turned up.

Each test names the thing that was actually wrong, not the code that was
changed, so a future refactor that reintroduces the behaviour fails here
rather than passing on a renamed helper.
"""
import dataclasses
import os

import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot import limits
from copybot.models import SkipReason
from copybot.strategy import Strategy

T0 = 1_787_000_000


def print_(price, size, ts, side="SELL", wallet="0xsomeone", asset="TOK1"):
    return {"price": price, "size": size, "timestamp": ts, "side": side,
            "proxyWallet": wallet, "asset": asset}


def limit_cfg(cfg, **over):
    base = dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[3.00],
                               limit_order_ttl_seconds=300)
    return dataclasses.replace(base, **over) if over else base


class Clock:
    """A clock the test can move. The tape only fills an order with prints
    inside its lifetime AND at or before `now`, so a frozen clock silently
    fills nothing -- which is indistinguishable from the bug under test."""

    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def build(db, cfg, books, tape=(), now=T0, metas=None, known_only=False):
    ex = FakeExecutor(books=books, tape=list(tape), ttl=cfg.limit_order_ttl_seconds)
    cl = FakeClient(books=books, metas=metas, known_only=known_only)
    clock = now if callable(now) else (lambda: now)
    return Strategy(cfg, db, ex, cl, clock=clock, run_id="run-test"), ex, cl


def resting_books(tokens, ask=0.32):
    """Books whose ask sits above his price, so every order genuinely rests."""
    return {t: make_book(token_id=t, asks=[(ask, 100_000)],
                         condition_id=f"0xcond-{t}")
            for t in tokens}


# --- resting fills must never be booked as filled without a position -------

def test_a_resting_fill_we_cannot_afford_is_not_recorded_as_filled(db, cfg):
    """The silent-drop path: cash out, order still marked filled.

    `_book_limit_fill` returned early when cash hit zero, but the order was
    persisted as fully filled anyway. On the next poll `gained` was zero, so
    those shares could never be booked: gone, with the fill-rate metric --
    the one number the limit experiment turns on -- counting them as wins.
    """
    poor = dataclasses.replace(
        limit_cfg(cfg, max_copies_per_token=1, stake_schedule=[1.0]),
        starting_capital_usd=6.00,
    )
    db.starting_capital_usd = 6.00
    tokens = [f"TOK{i}" for i in range(5)]
    books = resting_books(tokens)
    tape = [print_(0.29, 10_000, T0 + 10, asset=t) for t in tokens]
    clock = Clock()
    s, _, _ = build(db, poor, books, tape=tape, now=clock)

    for i, t in enumerate(tokens):
        s.process_trades([make_trade(token_id=t, condition_id=f"0xcond-{t}",
                                     price=0.29, shares=50.0, tx=f"0x{i}",
                                     ts=T0 + i)])
    clock.t = T0 + 20                       # the tape has now printed
    s.poll_resting_orders()

    assert any(r["filled_shares"] > 0 for r in
               db.conn.execute("SELECT filled_shares FROM resting_orders")), \
        "the tape must actually have filled something for this to test anything"

    rows = db.conn.execute(
        "SELECT token_id, filled_shares, filled_usd, status FROM resting_orders"
    ).fetchall()
    positions = {p["token_id"]: p for p in db.open_positions()}
    for row in rows:
        if row["filled_shares"] <= 1e-9:
            continue
        held = positions.get(row["token_id"])
        assert held is not None, (
            f"{row['token_id']} is recorded as filled with no position behind it")
        assert held["shares"] == pytest.approx(row["filled_shares"])
        assert held["cost_basis_usd"] == pytest.approx(row["filled_usd"], abs=1e-6)

    ok, parts = db.reconcile()
    assert ok, parts
    assert db.cash() >= -1e-9, "we cannot have spent more than we had"


def test_shares_blocked_by_cash_are_booked_once_cash_returns(db, cfg):
    """A rewound order is not a lost one -- the remainder stays reachable."""
    poor = dataclasses.replace(
        limit_cfg(cfg, max_copies_per_token=1, stake_schedule=[1.0]),
        starting_capital_usd=3.00,
    )
    db.starting_capital_usd = 3.00
    books = resting_books(["TOK1"])
    tape = [print_(0.29, 10_000, T0 + 10)]
    clock = Clock()
    s, _, _ = build(db, poor, books, tape=tape, now=clock)
    s.process_trades([make_trade(price=0.29, shares=50.0, ts=T0)])
    clock.t = T0 + 20

    # Spend the account behind the order's back, so its fill cannot be paid for.
    with db.tx() as conn:
        db.insert_execution(conn, token_id="OTHER", side="BUY", ts=T0 + 1,
                            shares=1.0, avg_fill=2.50, gross_usd=2.50,
                            fee_usd=0.0, net_usd=-2.50)
    assert db.cash() == pytest.approx(0.50)

    s.poll_resting_orders()
    row = db.conn.execute("SELECT * FROM resting_orders").fetchone()
    booked = db.open_position_for_token("TOK1")
    assert booked is not None
    assert row["filled_shares"] == pytest.approx(booked["shares"])
    assert row["status"] == "resting", "a part-paid order has not finished filling"

    # Cash comes back; the shares the tape already gave us are still ours to take.
    with db.tx() as conn:
        db.insert_execution(conn, token_id="OTHER", side="SELL", ts=T0 + 2,
                            shares=1.0, avg_fill=2.50, gross_usd=2.50,
                            fee_usd=0.0, net_usd=2.50)
    s.poll_resting_orders()
    after = db.open_position_for_token("TOK1")
    assert after["shares"] > booked["shares"]
    ok, parts = db.reconcile()
    assert ok, parts


# --- the cash gate has to see every resting order --------------------------

def test_resting_orders_cannot_promise_the_same_dollar_twice(db, cfg):
    """Five $3 orders against $6 of capital: each one saw the full $6 free."""
    poor = dataclasses.replace(
        limit_cfg(cfg, max_copies_per_token=1, stake_schedule=[1.0]),
        starting_capital_usd=6.00,
    )
    db.starting_capital_usd = 6.00
    tokens = [f"TOK{i}" for i in range(5)]
    s, _, _ = build(db, poor, resting_books(tokens))
    for i, t in enumerate(tokens):
        s.process_trades([make_trade(token_id=t, condition_id=f"0xcond-{t}",
                                     price=0.29, shares=50.0, tx=f"0x{i}",
                                     ts=T0 + i)])

    assert db.total_resting_exposure() <= 6.00 + 1e-6
    assert db.free_cash() >= -1e-6
    reasons = [r["reason"] for r in db.recent_skips(10)]
    assert SkipReason.NOT_ENOUGH_CASH.value in reasons


def test_free_cash_is_cash_minus_live_orders(db, cfg):
    s, _, _ = build(db, limit_cfg(cfg), resting_books(["TOK1"]))
    s.process_trades([make_trade(price=0.29, shares=50.0, ts=T0)])
    assert db.total_resting_exposure() > 0
    assert db.free_cash() == pytest.approx(db.cash() - db.total_resting_exposure())


# --- a dead order must not reserve budget forever --------------------------

def test_an_expired_partial_stops_reserving_its_remainder(db, cfg):
    """'partial' is terminal: the order expired, so its unfilled dollars are
    never going to be spent and must go back to the token's budget."""
    order = limits.RestingOrder(
        token_id="TOK1", condition_id="0xcond1", limit_price=0.20,
        usd_budget=1.02, target_shares=5.1, placed_ts=T0,
        expires_ts=T0 + 300, queue_ahead_shares=0.0, his_price=0.20,
        his_trade_key="k1", filled_shares=4.0, filled_usd=0.80,
    )
    with db.tx() as conn:
        db.upsert_resting(conn, order, run_id="r", status="resting",
                          stake_variant=3.00)
    assert db.resting_exposure("TOK1") == pytest.approx(0.22)

    with db.tx() as conn:
        db.upsert_resting(conn, order, run_id="r", status="partial",
                          stake_variant=3.00)
        db.close_resting(conn, "k1", "partial")
    assert db.resting_exposure("TOK1") == 0.0
    assert db.committed_on_token("TOK1") == pytest.approx(db.spent_on_token("TOK1"))


# --- heartbeat retention ---------------------------------------------------

def test_pruning_heartbeats_keeps_more_than_a_day(db):
    """A 5,000-row cap was 20.8 hours at a 15s poll, which structurally capped
    `days_without_stall` below 1 against a 30-day go-live gate."""
    from copybot.analytics import days_without_stall
    from copybot.db import now_ts

    now = now_ts()
    with db.tx() as conn:
        for i in range(0, 5 * 86400, 15):        # five days of 15s beats
            conn.execute(
                "INSERT INTO heartbeat (ts, loop_ok, trades_seen, copies_made, "
                "skips) VALUES (?,1,0,0,0)", (now - 5 * 86400 + i,))
    db.prune_heartbeats()
    kept = db.conn.execute("SELECT COUNT(*) AS n FROM heartbeat").fetchone()["n"]
    assert kept > 5000, "five days of beats must survive a prune"
    assert days_without_stall(db) > 4.0


def test_pruning_heartbeats_still_drops_ancient_ones(db):
    from copybot.db import now_ts
    now = now_ts()
    with db.tx() as conn:
        conn.execute("INSERT INTO heartbeat (ts, loop_ok, trades_seen, "
                     "copies_made, skips) VALUES (?,1,0,0,0)", (now - 200 * 86400,))
        conn.execute("INSERT INTO heartbeat (ts, loop_ok, trades_seen, "
                     "copies_made, skips) VALUES (?,1,0,0,0)", (now,))
    db.prune_heartbeats()
    rows = db.conn.execute("SELECT ts FROM heartbeat").fetchall()
    assert [r["ts"] for r in rows] == [now]


# --- his-position refresh must not invent our stake ------------------------

def test_refreshing_his_side_keeps_our_real_size_ratio(db, cfg):
    """The ratio is our money over his. Recomputing it from the config's
    headline stake reported a $1 token as if it were a $3 one."""
    # The $1 arm, and a book already offering at his price so the copy lands
    # immediately. Variants only exist in limit mode, which is where the wrong
    # constant did its damage.
    small = limit_cfg(cfg, stake_variants_usd=[1.00], max_copies_per_token=1,
                      stake_schedule=[1.0])
    books = {"TOK1": make_book(asks=[(0.10, 100_000)], bids=[(0.09, 100_000)])}
    s, _, _ = build(db, small, books)
    s.process_trades([make_trade(price=0.10, shares=50.0, tx="0x1", ts=T0)])
    opened = db.open_position_for_token("TOK1")
    assert opened is not None
    first = opened["size_ratio_vs_total"]

    # A second fill of his that we do not copy: the slot is used up, but his
    # side is refreshed. It must not overwrite our ratio with the $3 constant.
    s.process_trades([make_trade(price=0.09, shares=50.0, tx="0x2", ts=T0 + 1)])
    after = db.get_position(opened["id"])
    his_usd = after["his_total_usd"]
    assert after["size_ratio_vs_total"] == pytest.approx(
        after["cost_basis_opened"] / his_usd)
    assert after["size_ratio_vs_total"] < first, "his position grew, ours did not"
    assert after["size_ratio_vs_total"] != pytest.approx(
        cfg.stake_per_copy_usd / his_usd)


# --- unreadable activity rows ---------------------------------------------

def test_a_malformed_row_is_recorded_and_warned_about_once(db):
    row = {"transactionHash": "0xbad", "asset": "TOK1", "side": "SIDEWAYS",
           "price": 0.2, "size": 1.0, "timestamp": T0}
    assert db.record_malformed_activity(row, "bad or missing side") is True
    assert db.record_malformed_activity(row, "bad or missing side") is False
    assert db.skip_counts().get(SkipReason.MALFORMED_TRADE.value) == 1

    # It must never be mistaken for one of his fills.
    assert db.his_fills("TOK1") == []
    assert db.his_open_shares("TOK1") == 0.0

    other = dict(row, transactionHash="0xbad2")
    assert db.record_malformed_activity(other, "bad or missing side") is True
    assert db.skip_counts()[SkipReason.MALFORMED_TRADE.value] == 2


def test_the_engine_only_warns_once_per_bad_row(db, cfg, caplog, tmp_path):
    from copybot.engine import Engine

    bad = {"transactionHash": "0xbad", "asset": "TOK1", "conditionId": "0xcond1",
           "side": "SIDEWAYS", "price": 0.2, "size": 1.0, "timestamp": T0}

    class Client(FakeClient):
        def get_activity(self, wallet, limit=100, offset=0, seen_keys=None):
            return [bad]

    books = {"TOK1": make_book(asks=[(0.20, 100)])}
    market = dataclasses.replace(cfg, entry_mode="market")
    engine = Engine.__new__(Engine)
    engine.cfg = market
    engine.db = db
    engine.client = Client(books=books)
    engine.strategy = Strategy(market, db, FakeExecutor(books=books),
                               engine.client, clock=lambda: T0)
    engine.limit_mode = False

    with caplog.at_level("WARNING"):
        engine.poll_once()
        engine.poll_once()
        engine.poll_once()
    warnings = [r for r in caplog.records if "malformed activity row" in r.message]
    assert len(warnings) == 1, "a permanently bad row must not re-warn every poll"
    assert db.skip_counts()[SkipReason.MALFORMED_TRADE.value] == 1


# --- market state ----------------------------------------------------------

def test_a_closed_market_is_not_copied_however_good_its_book_looks(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    metas = {"0xcond1": make_meta(closed=True)}
    s, _, _ = build(db, dataclasses.replace(cfg, entry_mode="market"), books,
                    metas=metas)
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.copied == 0
    assert db.open_positions() == []
    assert db.recent_skips()[0]["reason"] == SkipReason.MARKET_CLOSED.value


def test_a_market_not_accepting_orders_is_not_copied(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    halted = dataclasses.replace(make_meta(), accepting_orders=False)
    s, _, _ = build(db, dataclasses.replace(cfg, entry_mode="market"), books,
                    metas={"0xcond1": halted})
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.MARKET_CLOSED.value


def test_metadata_we_cannot_fetch_is_not_taken_as_consent(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    s, _, _ = build(db, dataclasses.replace(cfg, entry_mode="market"), books,
                    known_only=True)
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.NO_MARKET_METADATA.value


def test_a_missing_acceptingorders_field_is_not_read_as_halted(db, cfg):
    """gamma omitting the flag is not gamma saying `false`."""
    quiet = dataclasses.replace(make_meta(), accepting_orders=None)
    books = {"TOK1": make_book(asks=[(0.20, 100_000)], bids=[(0.19, 100_000)])}
    s, _, _ = build(db, dataclasses.replace(cfg, entry_mode="market"), books,
                    metas={"0xcond1": quiet})
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.copied == 1


def test_gamma_parses_a_missing_flag_as_unknown():
    from copybot.polymarket import PolymarketClient
    client = PolymarketClient.__new__(PolymarketClient)
    row = {"conditionId": "0xc", "clobTokenIds": '["TOK1","TOK2"]',
           "outcomes": '["Yes","No"]', "outcomePrices": '["0.3","0.7"]'}
    assert client._parse_market(row).accepting_orders is None
    assert client._parse_market(dict(row, acceptingOrders=False)).accepting_orders is False
    assert client._parse_market(dict(row, acceptingOrders=True)).accepting_orders is True


# --- winners come from the settlement, not from proceeds -------------------

def test_a_half_sold_position_that_resolves_at_zero_is_not_a_winner(db, cfg):
    """With mirror_partial_sells on, proceeds_usd carries the sale. A position
    that then resolved worthless still had proceeds > 0."""
    from copybot.analytics import expected_vs_actual_winners

    with db.tx() as conn:
        position_id = db.insert_position(
            conn, token_id="TOK1", condition_id="0xc", question="Q",
            outcome="Yes", outcome_index=0, status="closed", opened_ts=T0,
            closed_ts=T0 + 10, shares=0.0, shares_opened=60.0,
            cost_basis_usd=0.0, cost_basis_opened=3.0, our_avg_fill=0.05,
            entry_band="0-10c", exit_path="resolution", proceeds_usd=1.60,
        )
        db.insert_execution(conn, position_id=position_id, token_id="TOK1",
                            condition_id="0xc", question="Q", side="SELL",
                            ts=T0 + 5, shares=30.0, avg_fill=0.053,
                            gross_usd=1.60, fee_usd=0.0, net_usd=1.60)
        db.insert_execution(conn, position_id=position_id, token_id="TOK1",
                            condition_id="0xc", question="Q", side="SETTLE",
                            ts=T0 + 10, shares=30.0, avg_fill=0.0,
                            gross_usd=0.0, fee_usd=0.0, net_usd=0.0)

    stats = expected_vs_actual_winners(db)
    assert stats["resolved"] == 1
    assert stats["actual"] == 0, "it settled at $0.00; the proceeds were a sale"
    assert stats["unmeasured"] == 0


# --- the instance lock has to name the incumbent ---------------------------

def test_a_second_instance_is_told_which_pid_holds_the_lock(tmp_path):
    from copybot.engine import AlreadyRunning, InstanceLock

    path = tmp_path / "copybot.sqlite3"
    held = InstanceLock(path).acquire()
    try:
        with pytest.raises(AlreadyRunning) as exc:
            InstanceLock(path).acquire()
        assert f"pid {os.getpid()}" in str(exc.value)
        assert held.path.read_text().strip() == str(os.getpid()), (
            "the loser must not erase the winner's record")
    finally:
        held.release()


# --- unplaceable is not the same as spent ----------------------------------

def test_an_order_below_the_exchange_minimum_says_so(db, cfg):
    """First fill on a token, nothing spent: the 5-share floor at 29c costs
    $1.45, which the $1 arm can never place. That is not budget exhaustion."""
    tiny = limit_cfg(cfg, stake_variants_usd=[1.00])
    books = {"TOK1": make_book(asks=[(0.32, 100_000)], min_size=5.0)}
    s, _, _ = build(db, tiny, books)
    c = s.process_trades([make_trade(price=0.29, shares=50.0, ts=T0)])
    assert c.copied == 0 and c.rested == 0
    skip = db.recent_skips()[0]
    assert skip["reason"] == SkipReason.BELOW_MIN_ORDER_SIZE.value
    assert "$0.00 of" not in (skip["detail"] or ""), \
        "it must not claim money was spent"


# --- the queue model is a knob, not a comment ------------------------------

def test_the_front_queue_model_ignores_size_resting_at_our_price():
    book = make_book(bids=[(0.30, 500), (0.29, 900)])
    assert limits.queue_ahead_shares(book, 0.30) == pytest.approx(500)
    assert limits.queue_ahead_shares(book, 0.30, "front") == 0.0
    assert limits.queue_ahead_shares(book, 0.29, "front") == pytest.approx(500)


def test_the_configured_queue_model_reaches_the_order(cfg):
    from copybot.executor import PaperExecutor

    book = make_book(bids=[(0.30, 500)])

    class Client:
        def get_book(self, token_id):
            return book

    back = PaperExecutor(limit_cfg(cfg), Client())
    front = PaperExecutor(limit_cfg(cfg, limit_queue_model="front"), Client())
    assert back.place_limit("TOK1", 3.0, 0.30, book=book,
                            now=T0).queue_ahead_shares == pytest.approx(500)
    assert front.place_limit("TOK1", 3.0, 0.30, book=book,
                             now=T0).queue_ahead_shares == 0.0


# --- polling a resting order must not price a fee that cannot exist --------

def test_polling_a_resting_order_asks_for_no_fee_rate(cfg):
    from copybot.executor import PaperExecutor

    class Client:
        def __init__(self):
            self.fee_calls = 0

        def get_trades(self, condition_id, limit=200):
            return [print_(0.30, 100, T0 + 5)]

        def fee_rate_for(self, condition_id, fallback):
            self.fee_calls += 1
            return fallback, True

    client = Client()
    ex = PaperExecutor(limit_cfg(cfg), client)
    order = limits.RestingOrder(
        token_id="TOK1", condition_id="0xcond1", limit_price=0.30,
        usd_budget=3.0, target_shares=10.0, placed_ts=T0,
        expires_ts=T0 + 300, queue_ahead_shares=0.0, his_price=0.30,
    )
    ex.begin_poll()
    ex.poll_limit(order, T0 + 10)
    ex.end_poll()
    assert client.fee_calls == 0, "a maker fill is free; no rate is needed"
    assert order.fee_usd == 0.0


# --- analytics are computed once per render --------------------------------

def test_stopping_rules_uses_what_it_is_handed(db, cfg):
    from copybot import analytics

    calls = {"curve": 0, "horizons": 0, "capture": 0}
    real_curve = analytics.capacity_curve
    real_horizons = analytics.clv_at_horizons
    real_capture = analytics.clv_summary

    def curve(*a, **k):
        calls["curve"] += 1
        return real_curve(*a, **k)

    def horizons(*a, **k):
        calls["horizons"] += 1
        return real_horizons(*a, **k)

    def capture(*a, **k):
        calls["capture"] += 1
        return real_capture(*a, **k)

    analytics.capacity_curve = curve
    analytics.clv_at_horizons = horizons
    analytics.clv_summary = capture
    try:
        pre_curve = real_curve(db)
        pre_horizons = real_horizons(db, [cfg.kill_clv_horizon_minutes])
        pre_capture = real_capture(db, cfg.clv_max_spread)
        calls.update(curve=0, horizons=0, capture=0)
        rules = analytics.stopping_rules(db, cfg, curve=pre_curve,
                                         horizons=pre_horizons,
                                         capture=pre_capture)
    finally:
        analytics.capacity_curve = real_curve
        analytics.clv_at_horizons = real_horizons
        analytics.clv_summary = real_capture

    assert calls == {"curve": 0, "horizons": 0, "capture": 0}
    assert rules["kill"], "it still produced the rules"


def test_stopping_rules_measures_a_kill_horizon_it_was_not_handed(db, cfg):
    from copybot.analytics import stopping_rules

    rules = stopping_rules(db, cfg, horizons=[])
    assert rules["kill"], "a horizon the caller omitted must still be measured"


# --- the dashboard must not write to the bot's database --------------------

def test_a_read_only_database_takes_no_write_lock(tmp_path):
    import sqlite3

    from copybot.db import Database

    path = tmp_path / "copybot.sqlite3"
    owner = Database(path, 150.0)
    try:
        reader = Database(path, 150.0, read_only=True)
        try:
            assert reader.read_only is True
            assert reader.cash() == pytest.approx(150.0)
            assert reader.reconcile()[0] is True
            with pytest.raises(sqlite3.OperationalError):
                reader.conn.execute(
                    "INSERT INTO heartbeat (ts, loop_ok, trades_seen, "
                    "copies_made, skips) VALUES (1,1,0,0,0)")
        finally:
            reader.close()
    finally:
        owner.close()


def test_a_read_only_database_still_comes_up_before_the_bot_has_run(tmp_path):
    from copybot.db import Database

    db = Database(tmp_path / "not-yet.sqlite3", 150.0, read_only=True)
    try:
        assert db.read_only is False, "there was no file to read"
        assert db.cash() == pytest.approx(150.0)
    finally:
        db.close()
