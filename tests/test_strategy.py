"""Tests 5, 7 and 8: partial sell mirroring, the entry price filter, and
resolution settlement. Plus closing-line capture, which is the primary metric.
"""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade, tweak

from copybot.models import SkipReason
from copybot.strategy import Strategy


def build(db, cfg, books=None, metas=None, fee_rate=0.0, now=1_787_000_000):
    ex = FakeExecutor(books=books, fee_rate=fee_rate)
    cl = FakeClient(books=books, metas=metas)
    return Strategy(cfg, db, ex, cl, clock=lambda: now), ex, cl


DEEP = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}


# --- 7. The entry price filter --------------------------------------------

def test_buy_at_49c_is_copied(db, cfg):
    books = {"TOK1": make_book(asks=[(0.49, 10_000)], bids=[(0.48, 10_000)])}
    s, _, _ = build(db, cfg, books=books)
    c = s.process_trades([make_trade(price=0.49, ts=1_787_000_000)])
    assert c.copied == 1
    assert len(db.open_positions()) == 1


def test_buy_at_51c_is_not_copied(db, cfg):
    books = {"TOK1": make_book(asks=[(0.51, 10_000)], bids=[(0.50, 10_000)])}
    s, ex, _ = build(db, cfg, books=books)
    c = s.process_trades([make_trade(price=0.51, ts=1_787_000_000)])
    assert c.copied == 0
    assert db.open_positions() == []
    assert db.recent_skips()[0]["reason"] == SkipReason.PRICE_ABOVE_MAX_ENTRY.value
    assert ex.calls == [], "must not even fetch a book for a rejected price"


def test_buy_exactly_at_50c_is_not_copied(db, cfg):
    """max_entry_price is exclusive: 'under this'."""
    s, _, _ = build(db, cfg, books=DEEP)
    c = s.process_trades([make_trade(price=0.50, ts=1_787_000_000)])
    assert c.copied == 0


def test_our_fill_above_our_max_is_skipped_even_when_his_was_cheap(db, cfg):
    """He bought at 5c; by the time we see it the book only offers 60c."""
    books = {"TOK1": make_book(asks=[(0.60, 10_000)], bids=[(0.59, 10_000)])}
    s, _, _ = build(db, cfg, books=books)
    c = s.process_trades([make_trade(price=0.05, ts=1_787_000_000)])
    assert c.copied == 0
    skip = db.recent_skips()[0]
    assert skip["reason"] == SkipReason.FILL_ABOVE_MAX.value
    assert skip["would_be_fill"] == pytest.approx(0.60)


# --- Other buy-path gates --------------------------------------------------

def test_stale_trade_is_skipped(db, cfg):
    s, _, _ = build(db, cfg, books=DEEP, now=1_787_000_000)
    c = s.process_trades([make_trade(price=0.20, ts=1_787_000_000 - 600)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.TRADE_TOO_OLD.value


def test_second_copy_of_same_token_is_skipped(db, cfg):
    s, _, _ = build(db, cfg, books=DEEP)
    s.process_trades([make_trade(price=0.20, tx="0xa", ts=1_787_000_000)])
    c = s.process_trades([make_trade(price=0.20, tx="0xb", ts=1_787_000_001)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.ALREADY_AT_MAX_COPIES.value
    assert len(db.open_positions()) == 1


def test_out_of_cash_is_skipped_with_that_reason(db, cfg):
    small = tweak(cfg, starting_capital_usd=4.0)
    db.starting_capital_usd = 4.0
    s, _, _ = build(db, small, books=DEEP)
    s.process_trades([make_trade(token_id="TOK1", price=0.20, tx="0xa", ts=1_787_000_000)])
    s2, _, _ = build(db, small, books={"TOK9": make_book(token_id="TOK9", asks=[(0.20, 10_000)])})
    c = s2.process_trades([make_trade(token_id="TOK9", price=0.20, tx="0xb", ts=1_787_000_000)])
    assert c.copied == 0
    assert db.recent_skips()[0]["reason"] == SkipReason.NOT_ENOUGH_CASH.value


def test_position_records_both_size_ratios_and_his_vwap(db, cfg):
    """He scaled in across three fills; we copy only the first."""
    s, _, _ = build(db, cfg, books=DEEP)
    s.process_trades([
        make_trade(price=0.10, shares=5.0, tx="0x1", ts=1_787_000_000),   # $0.50
        make_trade(price=0.02, shares=10.0, tx="0x2", ts=1_787_000_001),  # $0.20
        make_trade(price=0.01, shares=10.0, tx="0x3", ts=1_787_000_002),  # $0.10
    ])
    pos = db.open_positions()[0]
    # size_ratio is against the fill we copied: $3.00 / $0.50 = 6.0
    assert pos["size_ratio"] == pytest.approx(6.0)
    # his_position_usd_at_copy is FROZEN at what he held when we copied...
    assert pos["his_position_usd_at_copy"] == pytest.approx(0.50)
    # ...while his_position_usd_total keeps tracking him: 0.50 + 0.20 + 0.10
    assert pos["his_position_usd_total"] == pytest.approx(0.80)
    assert pos["his_fill_count"] == 3
    assert pos["size_ratio_vs_total"] == pytest.approx(3.0 / 0.80)


def test_his_vwap_keeps_improving_after_our_copy_is_frozen(db, cfg):
    """He opens at 10c and we copy that. He then averages down across two more
    fills we do not copy. His effective entry improves; ours cannot. That gap
    is only visible if his side keeps updating after the copy."""
    books = {"TOK1": make_book(asks=[(0.10, 10_000)], bids=[(0.09, 10_000)])}
    s, _, _ = build(db, cfg, books=books)

    s.process_trades([make_trade(price=0.10, shares=5.0, tx="0x1", ts=1_787_000_000)])
    pos = db.open_positions()[0]
    assert pos["his_vwap_entry"] == pytest.approx(0.10)
    assert pos["his_fill_count"] == 1
    assert pos["size_ratio_vs_total"] == pytest.approx(3.0 / 0.50)

    # He scales in: 45 @ 0.02 then 50 @ 0.02. We copy neither.
    s.process_trades([make_trade(price=0.02, shares=45.0, tx="0x2", ts=1_787_000_001)])
    s.process_trades([make_trade(price=0.02, shares=50.0, tx="0x3", ts=1_787_000_002)])

    pos = db.get_position(pos["id"])
    # VWAP = (5*0.10 + 45*0.02 + 50*0.02) / 100 = 2.40/100 = 0.024
    assert pos["his_vwap_entry"] == pytest.approx(0.024)
    assert pos["his_fill_count"] == 3
    assert pos["his_position_usd_total"] == pytest.approx(2.40)
    # His entry is now far better than ours, purely from scaling in.
    assert pos["his_vwap_entry"] < pos["our_avg_fill"]
    assert pos["slippage_vs_his_vwap"] > pos["slippage_vs_his_entry"]
    # And our $3 is now 1.25x his whole position, not 6x his opener.
    assert pos["size_ratio_vs_total"] == pytest.approx(3.0 / 2.40)
    assert pos["size_ratio"] == pytest.approx(6.0)


# --- 5. Partial sell mirroring ---------------------------------------------

def test_selling_half_his_position_sells_half_of_ours(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.30, 10_000)])}
    s, _, _ = build(db, cfg, books=books)
    s.process_trades([make_trade(price=0.20, shares=100.0, tx="0x1", ts=1_787_000_000)])
    pos = db.open_positions()[0]
    our_shares = pos["shares"]
    assert our_shares == pytest.approx(15.0)  # $3 at 0.20

    # He holds 100, sells 50 -> we mirror 50%.
    s.process_trades([make_trade(side="SELL", price=0.30, shares=50.0,
                                 tx="0x2", ts=1_787_000_010)])
    pos = db.get_position(pos["id"])
    assert pos["status"] == "open"
    assert pos["shares"] == pytest.approx(our_shares * 0.5)


def test_selling_a_quarter_mirrors_a_quarter(db, cfg):
    books = {"TOK1": make_book(asks=[(0.05, 10_000)], bids=[(0.06, 10_000)])}
    s, _, _ = build(db, cfg, books=books)
    s.process_trades([make_trade(price=0.05, shares=400.0, tx="0x1", ts=1_787_000_000)])
    our = db.open_positions()[0]["shares"]
    s.process_trades([make_trade(side="SELL", price=0.06, shares=100.0,
                                 tx="0x2", ts=1_787_000_010)])
    pos = db.open_positions()[0]
    assert pos["shares"] == pytest.approx(our * 0.75)


def test_selling_everything_closes_our_whole_position(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.30, 10_000)])}
    s, _, _ = build(db, cfg, books=books)
    s.process_trades([make_trade(price=0.20, shares=100.0, tx="0x1", ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]
    s.process_trades([make_trade(side="SELL", price=0.30, shares=100.0,
                                 tx="0x2", ts=1_787_000_010)])
    pos = db.get_position(pos_id)
    assert pos["status"] == "closed"
    assert pos["shares"] == 0.0
    assert pos["exit_path"] == "mirrored_sell"
    assert pos["realised_pnl_usd"] > 0


def test_residual_below_min_order_size_closes_the_whole_position(db, cfg):
    """We hold 15 shares. He sells 90% -> our residual would be 1.5 shares,
    under the 5-share exchange minimum and therefore unsellable live."""
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.30, 10_000)], min_size=5.0)}
    s, _, _ = build(db, cfg, books=books)
    s.process_trades([make_trade(price=0.20, shares=100.0, tx="0x1", ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]
    s.process_trades([make_trade(side="SELL", price=0.30, shares=90.0,
                                 tx="0x2", ts=1_787_000_010)])
    pos = db.get_position(pos_id)
    assert pos["status"] == "closed"
    assert pos["shares"] == 0.0
    assert "min order size" in (db.recent_executions(1)[0]["note"] or "")


def test_mirror_partial_sells_disabled_closes_everything(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.30, 10_000)])}
    s, _, _ = build(db, tweak(cfg, mirror_partial_sells=False), books=books)
    s.process_trades([make_trade(price=0.20, shares=100.0, tx="0x1", ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]
    s.process_trades([make_trade(side="SELL", price=0.30, shares=10.0,
                                 tx="0x2", ts=1_787_000_010)])
    assert db.get_position(pos_id)["status"] == "closed"


def test_his_sell_on_a_token_we_never_held_is_ignored(db, cfg):
    s, _, _ = build(db, cfg, books=DEEP)
    c = s.process_trades([make_trade(side="SELL", price=0.30, shares=10.0,
                                     tx="0x2", ts=1_787_000_010)])
    assert c.ignored == 1
    assert c.mirrored == 0
    assert db.cash() == pytest.approx(150.0)


def test_round_trip_reconciles(db, cfg):
    books = {"TOK1": make_book(asks=[(0.20, 10_000)], bids=[(0.30, 10_000)])}
    s, _, _ = build(db, cfg, books=books, fee_rate=0.05)
    s.process_trades([make_trade(price=0.20, shares=100.0, tx="0x1", ts=1_787_000_000)])
    s.process_trades([make_trade(side="SELL", price=0.30, shares=100.0,
                                 tx="0x2", ts=1_787_000_010)])
    ok, parts = db.reconcile()
    assert ok, parts


# --- 8. Resolution ---------------------------------------------------------

def test_winning_token_settles_at_one_dollar(db, cfg):
    metas = {"0xcond1": make_meta(prices=(0.30, 0.70))}
    s, _, cl = build(db, cfg, books=DEEP, metas=metas)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]
    shares = db.get_position(pos_id)["shares"]

    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    assert s.check_resolutions() == 1

    pos = db.get_position(pos_id)
    assert pos["status"] == "closed"
    assert pos["exit_path"] == "resolution"
    assert pos["proceeds_usd"] == pytest.approx(shares * 1.00)
    assert db.cash() == pytest.approx(150.0 - 3.0 + shares)
    ok, parts = db.reconcile()
    assert ok, parts


def test_losing_token_settles_at_zero(db, cfg):
    metas = {"0xcond1": make_meta(prices=(0.30, 0.70))}
    s, _, cl = build(db, cfg, books=DEEP, metas=metas)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]

    cl.metas["0xcond1"] = make_meta(prices=(0.0, 1.0), closed=True)
    s.check_resolutions()

    pos = db.get_position(pos_id)
    assert pos["status"] == "closed"
    assert pos["proceeds_usd"] == pytest.approx(0.0)
    assert pos["realised_pnl_usd"] == pytest.approx(-3.0)
    assert db.cash() == pytest.approx(147.0)
    ok, _ = db.reconcile()
    assert ok


def test_settlement_charges_no_fee(db, cfg):
    metas = {"0xcond1": make_meta(prices=(0.30, 0.70))}
    s, _, cl = build(db, cfg, books=DEEP, metas=metas, fee_rate=0.05)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    s.check_resolutions()
    settle = [e for e in db.recent_executions(10) if e["side"] == "SETTLE"][0]
    assert settle["fee_usd"] == pytest.approx(0.0)


def test_closed_but_ambiguous_price_does_not_settle(db, cfg):
    """A closed market showing 0.45 is not a resolution we understand."""
    metas = {"0xcond1": make_meta(prices=(0.30, 0.70))}
    s, _, cl = build(db, cfg, books=DEEP, metas=metas)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    cl.metas["0xcond1"] = make_meta(prices=(0.45, 0.55), closed=True)
    assert s.check_resolutions() == 0
    assert len(db.open_positions()) == 1


def test_unresolved_market_is_left_alone(db, cfg):
    metas = {"0xcond1": make_meta(prices=(0.30, 0.70), closed=False)}
    s, _, _ = build(db, cfg, books=DEEP, metas=metas)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    assert s.check_resolutions() == 0
    assert len(db.open_positions()) == 1
