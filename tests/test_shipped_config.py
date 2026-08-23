"""End-to-end against the real config.yaml — the settings actually deployed.

The other suites pin an explicit legacy config so they keep testing the thing
they were written for. That leaves the shipped configuration under-tested: one
test on the config being run is worth more than a hundred on one that isn't.

This walks the whole lifecycle under the real file: tranched entry under the
30c cap, a mirrored partial sell, and settlement, checking the ledger
reconciles at every step.
"""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.config import load_config
from copybot.models import SkipReason
from copybot.strategy import Strategy

T0 = 1_787_000_000


@pytest.fixture
def shipped():
    from pathlib import Path
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def build(db, cfg, price, now=T0):
    books = {"TOK1": make_book(asks=[(price, 100_000)], bids=[(price - 0.005, 100_000)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    return Strategy(cfg, db, ex, cl, clock=lambda: now), ex, cl


def buys(db):
    return [e for e in db.recent_executions(50) if e["side"] == "BUY"]


# --- the settings themselves ----------------------------------------------

def test_shipped_config_is_the_one_we_think_it_is(shipped):
    assert shipped.mode == "paper"
    assert shipped.max_entry_price == 0.30
    assert shipped.shadow_band_max_price == 0.50
    assert shipped.stake_per_copy_usd == 3.00
    assert shipped.starting_capital_usd == 150.00
    assert abs(sum(shipped.stake_schedule) - 1.0) < 1e-9
    assert shipped.max_copies_per_token <= len(shipped.stake_schedule)
    assert shipped.vwap_breakeven_ratio == 1.395
    assert shipped.fee_rate_fallback > 0
    assert shipped.dashboard_host == "127.0.0.1"


def test_shipped_config_refuses_live_mode(shipped):
    import dataclasses

    from copybot.config import ConfigError, _validate
    with pytest.raises(ConfigError, match="not implemented"):
        _validate(dataclasses.replace(shipped, mode="live"))


def test_shipped_config_rests_rather_than_crossing(shipped):
    """The whole point of this run. Market orders paid 11-197% worse than he
    did against a break-even of 1.395x, so entries rest at his price now."""
    assert shipped.entry_mode == "limit"
    assert shipped.limit_order_ttl_seconds > 0
    assert shipped.limit_queue_model == "back", "we join at the back of the queue"
    assert len(shipped.stake_variants_usd) >= 1
    assert max(shipped.stake_variants_usd) <= shipped.stake_per_copy_usd + 1e-9, \
        "no variant may exceed the per-token cap"


def test_shipped_config_refuses_an_unknown_entry_mode(shipped):
    import dataclasses

    from copybot.config import ConfigError, _validate
    with pytest.raises(ConfigError, match="entry_mode"):
        _validate(dataclasses.replace(shipped, entry_mode="aggressive"))


# --- buy path --------------------------------------------------------------

def test_cheap_entry_tranches_three_ways_under_the_cap(db, shipped):
    """At 2c the budget supports many tranches, so the schedule applies in
    full and the total never exceeds one budget."""
    s, ex, _ = build(db, shipped, 0.02)
    cap = s.budget_for("TOK1", ex.books["TOK1"], 0.02)
    for i in range(5):
        s.process_trades([make_trade(price=0.02, tx=f"0x{i}", ts=T0 + i)])
    rows = buys(db)
    assert len(rows) == shipped.max_copies_per_token
    total = sum(-e["net_usd"] for e in rows)
    assert total == pytest.approx(cap, abs=0.01)
    assert cap <= shipped.stake_per_copy_usd + 1e-9
    ok, parts = db.reconcile()
    assert ok, parts


def test_expensive_entry_tranches_twice_when_resting(db, shipped):
    """29c is inside his +45.4% band. Resting pays no fee, so the 5-share floor
    is $1.45 and two of those fit in $3 at 5.0 shares each -- follow-him-down
    survives up here, which matters most where the absolute price gap is widest.

    The stake variants are pinned so this stays a test of the floor arithmetic
    rather than of which arm the token hashes to.
    """
    import dataclasses
    cfg = dataclasses.replace(shipped, entry_mode="limit", stake_variants_usd=[3.00])
    # Asks sit ABOVE his price, so our order genuinely rests instead of crossing.
    books = {"TOK1": make_book(asks=[(0.30, 100_000)], bids=[(0.28, 40.0)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(cfg, db, ex, cl, clock=lambda: T0)
    for i in range(4):
        s.process_trades([make_trade(price=0.29, tx=f"0x{i}", ts=T0 + i)])

    resting = db.open_resting()
    assert len(resting) == 2, "collapsing to one order disables follow-him-down at 29c"
    assert all(r["target_shares"] >= 5.0 for r in resting)
    assert sum(r["usd_budget"] for r in resting) <= shipped.stake_per_copy_usd + 1e-6
    assert buys(db) == [], "resting commits no cash until the tape fills it"
    assert db.cash() == pytest.approx(shipped.starting_capital_usd)


def test_expensive_entry_collapses_to_one_when_crossing(db, shipped):
    """Crossing pays the taker fee, which lifts the same floor to $1.50. Two of
    those do not fit in $3, so market mode genuinely gets one fill at 29c.
    That is a real cost of crossing, not a sizing bug."""
    s, _, _ = build(db, shipped, 0.29)
    for i in range(4):
        s.process_trades([make_trade(price=0.29, tx=f"0x{i}", ts=T0 + i)])
    rows = buys(db)
    assert len(rows) == 1
    assert sum(-e["net_usd"] for e in rows) <= shipped.stake_per_copy_usd + 1e-6


def test_entry_above_the_cap_is_refused(db, shipped):
    s, _, _ = build(db, shipped, 0.35)
    c = s.process_trades([make_trade(price=0.35, ts=T0)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.PRICE_ABOVE_MAX_ENTRY.value


def test_shadow_band_signal_records_a_ladder_without_spending(db, shipped):
    """35c is outside the traded universe but inside the shadow band, so it
    must still leave measurements behind."""
    s, _, _ = build(db, shipped, 0.35)
    s.process_trades([make_trade(price=0.35, ts=T0)])
    assert buys(db) == []
    assert db.cash() == pytest.approx(shipped.starting_capital_usd)


# --- full lifecycle --------------------------------------------------------

def test_buy_then_mirrored_sell_then_reconcile(db, shipped):
    s, ex, cl = build(db, shipped, 0.05)
    cap = s.budget_for("TOK1", ex.books["TOK1"], 0.05)
    for i in range(3):
        s.process_trades([make_trade(price=0.05, shares=100.0, tx=f"0x{i}", ts=T0 + i)])
    opened = db.open_position_for_token("TOK1")
    assert opened is not None
    staked = sum(-e["net_usd"] for e in buys(db))
    assert staked == pytest.approx(cap, abs=0.01)

    ex.books["TOK1"] = make_book(asks=[(0.11, 100_000)], bids=[(0.10, 100_000)])
    s.process_trades([make_trade(side="SELL", price=0.10, shares=150.0,
                                 tx="0xs", ts=T0 + 20)])
    after = db.get_position(opened["id"])
    assert after["shares"] < opened["shares"], "his 50% sell must be mirrored"
    ok, parts = db.reconcile()
    assert ok, parts


def test_buy_then_resolution_settles_and_reconciles(db, shipped):
    s, _, cl = build(db, shipped, 0.05)
    for i in range(3):
        s.process_trades([make_trade(price=0.05, tx=f"0x{i}", ts=T0 + i)])
    pos_id = db.open_position_for_token("TOK1")["id"]
    shares = db.get_position(pos_id)["shares"]
    staked = sum(-e["net_usd"] for e in buys(db))

    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    assert s.check_resolutions() == 1

    closed = db.get_position(pos_id)
    assert closed["status"] == "closed"
    assert closed["exit_path"] == "resolution"
    assert closed["proceeds_usd"] == pytest.approx(shares * 1.00)
    assert db.cash() == pytest.approx(
        shipped.starting_capital_usd - staked + shares, abs=1e-6)
    ok, parts = db.reconcile()
    assert ok, parts


def test_losing_resolution_under_shipped_config(db, shipped):
    s, _, cl = build(db, shipped, 0.05)
    s.process_trades([make_trade(price=0.05, ts=T0)])
    staked = sum(-e["net_usd"] for e in buys(db))
    cl.metas["0xcond1"] = make_meta(prices=(0.0, 1.0), closed=True)
    s.check_resolutions()
    assert db.cash() == pytest.approx(shipped.starting_capital_usd - staked)
    ok, _ = db.reconcile()
    assert ok


def test_capital_runs_out_and_says_so(db, shipped):
    """$150 buys somewhere between 50 and 150 fully-funded tokens depending on
    which stake each one draws. However many it is, the one after the last must
    be refused for cash, and the account must never be overdrawn."""
    s, ex, cl = build(db, shipped, 0.02)
    for i in range(170):
        ex.books[f"T{i}"] = make_book(token_id=f"T{i}", asks=[(0.02, 100_000)],
                                      bids=[(0.015, 100_000)])
        cl.books[f"T{i}"] = ex.books[f"T{i}"]
        for j in range(shipped.max_copies_per_token):
            s.process_trades([make_trade(token_id=f"T{i}", price=0.02,
                                         tx=f"0x{i}_{j}", ts=T0 + i * 10 + j)])
        assert db.cash() >= 0.0, "cash must never go negative"
    reasons = {r["reason"] for r in db.recent_skips(1000)}
    assert SkipReason.NOT_ENOUGH_CASH.value in reasons
    spent = sum(-e["net_usd"] for e in buys(db))
    assert spent <= shipped.starting_capital_usd + 1e-6
    ok, parts = db.reconcile()
    assert ok, parts


# --- the leak that broke reconciliation in production ----------------------

def test_partial_sell_of_a_full_close_keeps_the_unsold_shares(db, shipped):
    """He sells everything, but the bid side can only absorb part of our
    position. Closing on INTENT rather than on what actually sold zeroed the
    basis of shares we still held, and the money vanished from the ledger --
    this is what put the deployed database $0.06 out of balance."""
    import dataclasses
    # The stake is pinned so the position is comfortably larger than the bids,
    # which is the situation being reproduced; the variant it would otherwise
    # draw is irrelevant to a basis leak.
    cfg = dataclasses.replace(shipped, stake_variants_usd=[3.00])
    books = {"TOK1": make_book(asks=[(0.05, 100_000)], bids=[(0.09, 8)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(cfg, db, ex, cl, clock=lambda: T0)

    s.process_trades([make_trade(price=0.05, shares=100.0, tx="0x1", ts=T0)])
    pos = db.open_position_for_token("TOK1")
    held = pos["shares"]
    assert held > 8, "the position must exceed what the bids can absorb"

    s.process_trades([make_trade(side="SELL", price=0.09, shares=100.0,
                                 tx="0x2", ts=T0 + 10)])
    after = db.get_position(pos["id"])
    sold = [e for e in db.recent_executions(9) if e["side"] == "SELL"][0]

    assert sold["shares"] < held, "the book should only have absorbed part"
    assert after["status"] == "open", "unsold shares mean the position is still open"
    assert after["shares"] == pytest.approx(held - sold["shares"])
    assert after["cost_basis_usd"] > 0, "the unsold shares keep their cost basis"

    ok, parts = db.reconcile()
    assert ok, f"a partial close must not leak basis: {parts}"


def test_full_close_still_closes_when_the_book_can_absorb_it(db, shipped):
    """The fix must not stop a genuine full close from closing."""
    books = {"TOK1": make_book(asks=[(0.05, 100_000)], bids=[(0.09, 100_000)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(shipped, db, ex, cl, clock=lambda: T0)

    s.process_trades([make_trade(price=0.05, shares=100.0, tx="0x1", ts=T0)])
    pos_id = db.open_position_for_token("TOK1")["id"]
    s.process_trades([make_trade(side="SELL", price=0.09, shares=100.0,
                                 tx="0x2", ts=T0 + 10)])
    after = db.get_position(pos_id)
    assert after["status"] == "closed"
    assert after["shares"] == 0.0
    assert after["cost_basis_usd"] == 0.0
    assert after["exit_path"] == "mirrored_sell"
    ok, parts = db.reconcile()
    assert ok, parts
