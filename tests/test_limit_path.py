"""The limit path end to end: rest at his price, fill from the tape, or expire.

This is the experiment. Market orders crossed the spread ~15s behind him and
paid 11-197% worse on his own picks, against a break-even of our VWAP / his
VWAP = 1.395. A resting order at his exact price either fills at his price or
does not fill at all, so the question stops being "how much worse did we pay"
and becomes "what fraction of his buys did we get". These tests pin both
answers: the fills that happen land at his price exactly, and the ones that do
not are recorded as misses rather than quietly disappearing.
"""
import dataclasses

import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.strategy import Strategy

T0 = 1_787_000_000
HIM = "0x6297b93ea37ff92a57fd636410f3b71ebf74517e"


@pytest.fixture
def limit_cfg(cfg):
    """The shipped config in limit mode, with the stake pinned.

    Variants are a separate experiment; pinning them here keeps these tests
    about the resting model rather than about which arm a token hashes to.
    """
    return dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[3.00],
                               limit_order_ttl_seconds=300)


def resting_book(ask=0.22, queue=0.0, limit=0.20):
    """A book whose ask sits ABOVE his price, so our order genuinely rests.

    `queue` is the size already bidding AT his price -- the shares that must
    trade before ours. Zero means we are at the front, which is the only way to
    isolate the tape logic from the queue logic; the queue is tested separately.
    """
    bids = [(limit, queue)] if queue else []
    return {"TOK1": make_book(asks=[(ask, 100_000)], bids=bids)}


def build(db, cfg, books, tape=(), now=T0, ttl=300):
    ex = FakeExecutor(books=books, tape=list(tape), ttl=ttl)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    return Strategy(cfg, db, ex, cl, clock=lambda: now, run_id="run-test"), ex, cl


def print_(price, size, ts, side="SELL", wallet="0xsomeone"):
    return {"price": price, "size": size, "timestamp": ts, "side": side,
            "proxyWallet": wallet}


# --- placing ---------------------------------------------------------------

def test_resting_commits_no_cash_until_the_tape_fills_it(db, limit_cfg):
    s, _, _ = build(db, limit_cfg, resting_book())
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.rested == 1 and c.copied == 0
    assert db.cash() == pytest.approx(limit_cfg.starting_capital_usd)
    assert db.open_positions() == []
    order = db.open_resting()[0]
    assert order["limit_price"] == pytest.approx(0.20), "must rest at HIS price"
    assert order["status"] == "resting"


def test_a_marketable_book_is_crossed_rather_than_rested(db, limit_cfg):
    """If the book is already offering at or below his price, resting would
    cross anyway. That is a windfall, not slippage -- take it, pay the taker
    fee, and record it as a distinct path so it never flatters the fill rate."""
    books = {"TOK1": make_book(asks=[(0.19, 10_000)], bids=[(0.18, 10_000)])}
    s, _, _ = build(db, limit_cfg, books)
    c = s.process_trades([make_trade(price=0.20, ts=T0)])
    assert c.copied == 1 and c.rested == 0
    pos = db.open_position_for_token("TOK1")
    assert pos["our_avg_fill"] <= 0.20 + 1e-9, "crossing inside must never pay worse"
    assert db.open_resting() == []


# --- filling ---------------------------------------------------------------

def test_tape_at_our_price_fills_us_at_his_price_exactly(db, limit_cfg):
    tape = [print_(0.20, 500.0, T0 + 60)]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=tape)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    stats = s.poll_resting_orders()

    assert stats["filled"] == 1
    pos = db.open_position_for_token("TOK1")
    assert pos["our_avg_fill"] == pytest.approx(0.20), \
        "a resting fill happens at our limit, never worse"
    assert pos["entry_fee_usd"] == pytest.approx(0.0), "maker fills pay no fee"
    ok, parts = db.reconcile()
    assert ok, parts


def test_prints_above_our_limit_never_fill_us(db, limit_cfg):
    """The ask quoting through our price is not the market trading at it."""
    tape = [print_(0.21, 5_000.0, T0 + 60), print_(0.25, 5_000.0, T0 + 61)]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=tape)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    assert db.open_positions() == []
    assert db.open_resting()[0]["filled_shares"] == pytest.approx(0.0)


def test_his_own_print_cannot_fill_us(db, limit_cfg):
    """His buy is not the seller on the other side of our bid, and counting it
    would mean trading against the wallet we copy."""
    tape = [print_(0.20, 5_000.0, T0 + 60, wallet=HIM)]
    cfg = dataclasses.replace(limit_cfg, target_wallet=HIM)
    s, ex, _ = build(db, cfg, resting_book(), tape=tape)
    ex.exclude_wallet = HIM
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    assert db.open_positions() == [], "his own fill must not fill ours"


def test_prints_before_placement_cannot_fill_us(db, limit_cfg):
    """Look-ahead in reverse: a trade from before we placed cannot have filled
    an order that did not exist yet."""
    tape = [print_(0.20, 5_000.0, T0 - 60)]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=tape)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    assert db.open_positions() == []


def test_queue_ahead_must_trade_through_before_we_fill(db, limit_cfg):
    """We join at the back. 400 shares resting at our price is 400 shares of
    tape we get nothing from."""
    books = resting_book(queue=400.0)
    s, _, _ = build(db, limit_cfg, books, tape=[print_(0.20, 300.0, T0 + 30)])
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()
    assert db.open_positions() == [], "300 of 400 queued shares is not our turn yet"

    # Another 200 prints: the tape is now 100 shares past the queue, which is
    # more than our whole order, so we fill -- and only then.
    order = db.open_resting()[0]
    s.executor.tape.append(print_(0.20, 200.0, T0 + 90))
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    pos = db.open_position_for_token("TOK1")
    assert pos is not None
    assert pos["shares"] == pytest.approx(order["target_shares"], abs=1e-6)
    assert pos["our_avg_fill"] == pytest.approx(0.20)


# --- expiry ----------------------------------------------------------------

def test_an_unfilled_order_expires_and_frees_its_slot(db, limit_cfg):
    s, _, _ = build(db, limit_cfg, resting_book(), ttl=300)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    assert db.committed_on_token("TOK1") > 0, "a resting order is committed money"

    s.clock = lambda: T0 + 400
    stats = s.poll_resting_orders()
    assert stats["expired"] == 1
    assert db.open_resting() == []
    assert db.committed_on_token("TOK1") == pytest.approx(0.0), \
        "an order that never filled must release the budget it was holding"
    assert db.cash() == pytest.approx(limit_cfg.starting_capital_usd)


def test_a_partial_fill_at_expiry_keeps_what_filled(db, limit_cfg):
    books = resting_book(queue=0.0)
    s, _, _ = build(db, limit_cfg, books, tape=[print_(0.20, 4.0, T0 + 10)], ttl=300)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 400
    stats = s.poll_resting_orders()
    assert stats["partial"] == 1
    pos = db.open_position_for_token("TOK1")
    assert pos["shares"] == pytest.approx(4.0)
    ok, parts = db.reconcile()
    assert ok, parts


# --- the cap holds across resting orders -----------------------------------

def test_resting_orders_count_against_the_per_token_cap(db, limit_cfg):
    """Four of his fills must not put four orders against a three-order budget.
    Cash has not moved yet, but if they all fill, they all fill."""
    s, _, _ = build(db, limit_cfg, resting_book())
    for i in range(4):
        s.process_trades([make_trade(price=0.20, tx=f"0x{i}", ts=T0 + i)])
    orders = db.open_resting()
    assert sum(o["usd_budget"] for o in orders) <= limit_cfg.stake_per_copy_usd + 1e-6
    assert len(orders) <= limit_cfg.max_copies_per_token


def test_filled_and_resting_together_never_exceed_the_budget(db, limit_cfg):
    """One order fills, then he buys again: the second order may only claim
    what the first one left."""
    tape = [print_(0.20, 500.0, T0 + 5)]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=tape)
    s.process_trades([make_trade(price=0.20, tx="0xa", ts=T0)])
    s.clock = lambda: T0 + 10
    s.poll_resting_orders()

    s.process_trades([make_trade(price=0.20, tx="0xb", ts=T0 + 11)])
    committed = db.committed_on_token("TOK1")
    assert committed <= limit_cfg.stake_per_copy_usd + 1e-6, \
        f"filled plus resting is ${committed:.4f}, over the cap"


# --- fill rate, the number the experiment turns on -------------------------

def test_fill_rate_counts_both_conventions(db, limit_cfg):
    """Which `side` label means "a seller hit the bid" is not established, so
    the opposite reading is carried alongside rather than assumed away."""
    tape = [print_(0.20, 500.0, T0 + 30, side="BUY")]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=tape)
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 400
    s.poll_resting_orders()

    row = db.conn.execute(
        "SELECT filled_shares, alt_filled_shares, prints_observed, "
        "alt_prints_observed FROM resting_orders WHERE token_id='TOK1'"
    ).fetchone()
    assert row["filled_shares"] == pytest.approx(0.0), \
        "under the configured convention a BUY print consumed no bid queue"
    assert row["alt_filled_shares"] > 0, \
        "the opposite reading must be recorded, not discarded"
    assert row["alt_prints_observed"] == 1


def test_fill_rate_stats_report_orders_and_shares(db, limit_cfg):
    filled = [print_(0.20, 500.0, T0 + 30)]
    s, _, _ = build(db, limit_cfg, resting_book(), tape=filled)
    s.process_trades([make_trade(price=0.20, tx="0xa", ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()

    # A second order that never fills.
    s.executor.tape = []
    s.clock = lambda: T0 + 100
    s.process_trades([make_trade(price=0.20, tx="0xb", ts=T0 + 100)])
    s.clock = lambda: T0 + 900
    s.poll_resting_orders()

    stats = db.fill_rate_stats()
    assert stats["orders"] == 2
    assert stats["any_fill"] == 1
    assert stats["fill_rate"] == pytest.approx(0.5)
    assert 0.0 < stats["share_fill_rate"] < 1.0


def test_prints_on_the_other_outcome_never_fill_us(db, limit_cfg):
    """The tape is fetched per market, and a market has two tokens. A 20c print
    on the complementary outcome is a different book with a different queue --
    counting it would manufacture fills out of the other side's volume."""
    other = dict(print_(0.20, 5_000.0, T0 + 30))
    other["asset"] = "TOK2"
    s, _, _ = build(db, limit_cfg, resting_book(), tape=[other])
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    assert db.open_positions() == []
    assert db.open_resting()[0]["consumed_shares"] == pytest.approx(0.0)


def test_prints_on_our_own_token_still_fill_us(db, limit_cfg):
    ours = dict(print_(0.20, 500.0, T0 + 30))
    ours["asset"] = "TOK1"
    s, _, _ = build(db, limit_cfg, resting_book(), tape=[ours])
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 120
    s.poll_resting_orders()
    assert db.open_position_for_token("TOK1") is not None


# --- stake as a tested variable --------------------------------------------

def test_each_token_keeps_its_stake_across_a_restart(db, cfg, tmp_path):
    """The variant is assigned by hashing the token, so a restart cannot move a
    token from one arm to the other and blur the comparison."""
    import dataclasses
    from copybot.db import Database
    cfg = dataclasses.replace(cfg, entry_mode="limit")
    path = tmp_path / "variants.sqlite3"
    d1 = Database(path, cfg.starting_capital_usd)
    s1, _, _ = build(d1, cfg, resting_book())
    first = s1.stake_variant("TOK1")
    d1.close()

    d2 = Database(path, cfg.starting_capital_usd)
    s2, _, _ = build(d2, cfg, resting_book())
    assert s2.stake_variant("TOK1") == first
    d2.close()


def test_the_arms_are_spread_across_tokens(cfg):
    """A hash that sent every token to one arm would produce a comparison of
    one thing against nothing."""
    import dataclasses
    from copybot.db import Database
    cfg = dataclasses.replace(cfg, entry_mode="limit")
    s = Strategy(cfg, None, None, None, clock=lambda: T0)
    counts = {}
    for i in range(300):
        v = s.stake_variant(f"token-{i}")
        counts[v] = counts.get(v, 0) + 1
    assert set(counts) == set(cfg.stake_variants_usd)
    assert min(counts.values()) > 300 / len(cfg.stake_variants_usd) / 2


def test_a_stake_too_small_to_place_moves_up_rather_than_out(db, cfg):
    """At the 5-share minimum a $1 order is unplaceable above 20c. Skipping
    those would drop the whole 20-30c band out of the $1 arm instead of
    measuring it, so the token moves up to the smallest stake that fits."""
    import dataclasses
    cfg = dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[1.00])
    s, _, _ = build(db, cfg, resting_book(ask=0.31, limit=0.29))
    book = s.executor.get_book("TOK1")
    assert s.budget_for("TOK1", book, 0.02) == pytest.approx(1.00), \
        "at 2c a $1 order is placeable and must stay in its own arm"
    # At 29c the floor is 5 x 0.29 = $1.45 and no larger variant exists, so the
    # budget falls back to the nominal and the fill is refused rather than
    # silently doubled.
    assert s.budget_for("TOK1", book, 0.29) == pytest.approx(1.00)

    cfg3 = dataclasses.replace(cfg, stake_variants_usd=[1.00, 3.00])
    s3, _, _ = build(db, cfg3, resting_book(ask=0.31, limit=0.29))
    assert s3.budget_for("TOK1", s3.executor.get_book("TOK1"), 0.29) == 3.00


def test_the_breakdown_reports_each_stake_separately(db, cfg):
    import dataclasses
    cfg = dataclasses.replace(cfg, entry_mode="limit", stake_variants_usd=[1.00])
    s, _, _ = build(db, cfg, resting_book(), tape=[print_(0.20, 500.0, T0 + 30)])
    s.process_trades([make_trade(price=0.20, ts=T0)])
    s.clock = lambda: T0 + 60
    s.poll_resting_orders()

    rows = db.variant_breakdown()
    assert len(rows) == 1
    assert rows[0]["stake"] == pytest.approx(1.00)
    assert rows[0]["orders"] == 1
    assert rows[0]["fill_rate"] == pytest.approx(1.0)
    assert rows[0]["positions"] == 1
