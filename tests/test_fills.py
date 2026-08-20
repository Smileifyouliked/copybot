"""Tests 1 and 2: book walking and thin-book rejection.

Expected values here are computed by hand in the docstrings, not by running the
code and pasting the output. A test that asserts what the code already does
cannot catch the bug this module exists to prevent.
"""
import pytest

from copybot.fees import FeeModel, effective_rate_of_notional, taker_fee_usd
from copybot.fills import LookAheadError, simulate_buy, simulate_sell
from copybot.models import BookLevel, OrderBook, SkipReason

NO_FEE = FeeModel(rate=0.0)
WEATHER_FEE = FeeModel(rate=0.05)


def book(asks=(), bids=(), tick=0.001, min_size=5.0, ts=0):
    """asks/bids given as (price, size), any order -- OrderBook sorts them."""
    return OrderBook(
        token_id="TOK",
        bids=sorted([BookLevel(p, s) for p, s in bids], key=lambda l: l.price, reverse=True),
        asks=sorted([BookLevel(p, s) for p, s in asks], key=lambda l: l.price),
        tick_size=tick, min_order_size=min_size, timestamp_ms=ts,
    )


# --- 1. Walking multiple levels -------------------------------------------
#
#   asks: 5 @ 0.20, 5 @ 0.25, 100 @ 0.30   budget $3.00, no fees
#     level 1: 3.00/0.20 = 15 affordable, only 5 there -> take 5, spend 1.00
#     level 2: 2.00/0.25 =  8 affordable, only 5 there -> take 5, spend 1.25
#     level 3: 0.75/0.30 = 2.5 affordable, 100 there   -> take 2.5, spend 0.75
#   total 12.5 shares for $3.00 -> VWAP 0.24

def test_walk_produces_hand_computed_vwap():
    r = simulate_buy(book(asks=[(0.20, 5), (0.25, 5), (0.30, 100)]), 3.00,
                     NO_FEE, max_fill_price=0.50)
    assert r.filled
    assert r.shares == pytest.approx(12.5)
    assert r.gross_usd == pytest.approx(3.00)
    assert r.avg_price == pytest.approx(0.24)
    assert r.levels_consumed == 3
    assert r.worst_price == pytest.approx(0.30)


def test_walk_is_worse_than_the_top_of_book():
    """The whole point: depth costs money. Filling at the best ask would have
    bought 15 shares; walking the real book buys 12.5."""
    r = simulate_buy(book(asks=[(0.20, 5), (0.25, 5), (0.30, 100)]), 3.00,
                     NO_FEE, max_fill_price=0.50)
    naive_best_ask = 3.00 / 0.20
    assert naive_best_ask == pytest.approx(15.0)
    assert r.shares < naive_best_ask
    assert r.avg_price > 0.20


def test_walk_never_fills_at_the_mid():
    """Mid on this book is 0.15, which would buy 20 shares -- 60% more than the
    book can actually supply at that money."""
    b = book(asks=[(0.20, 5), (0.25, 5), (0.30, 100)], bids=[(0.10, 100)])
    assert b.mid == pytest.approx(0.15)
    r = simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50)
    assert r.shares == pytest.approx(12.5)
    assert r.shares < 3.00 / b.mid


def test_single_level_book_fills_at_that_level():
    r = simulate_buy(book(asks=[(0.25, 1000)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert r.filled
    assert r.avg_price == pytest.approx(0.25)
    assert r.shares == pytest.approx(12.0)
    assert r.levels_consumed == 1


def test_raw_clob_ask_ordering_does_not_break_the_walk():
    """The CLOB returns asks worst-first (0.99 … 0.06). Walking from index 0
    would fill at 0.99. OrderBook.from_clob must re-sort."""
    payload = {
        "asset_id": "TOK",
        "asks": [{"price": "0.99", "size": "1000"}, {"price": "0.30", "size": "100"},
                 {"price": "0.25", "size": "5"}, {"price": "0.20", "size": "5"}],
        "bids": [{"price": "0.01", "size": "50"}, {"price": "0.10", "size": "20"}],
        "tick_size": "0.01", "min_order_size": "5",
    }
    b = OrderBook.from_clob(payload)
    assert b.best_ask == pytest.approx(0.20)
    assert b.best_bid == pytest.approx(0.10)
    r = simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50)
    assert r.avg_price == pytest.approx(0.24)
    assert r.avg_price < 0.99


# --- 2. Thin books are skipped, never fantasy-filled -----------------------

def test_thin_book_skips_instead_of_filling():
    """Book holds 5 @ 0.20 = $1.00. A $3 order cannot be placed."""
    r = simulate_buy(book(asks=[(0.20, 5)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert not r.filled
    assert r.skip_reason is SkipReason.BOOK_TOO_THIN
    assert r.shares == 0.0
    assert r.net_usd == 0.0


def test_thin_book_skip_reports_the_depth_it_had():
    r = simulate_buy(book(asks=[(0.20, 5), (0.25, 4)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert not r.filled
    assert r.depth_available_usd == pytest.approx(5 * 0.20 + 4 * 0.25)
    assert "2.00" in r.detail
    assert r.would_be_avg_price > 0  # what we'd have paid, for the record


def test_empty_book_skips_and_does_not_crash():
    r = simulate_buy(book(asks=[]), 3.00, NO_FEE, max_fill_price=0.50)
    assert not r.filled
    assert r.skip_reason is SkipReason.EMPTY_BOOK
    assert r.avg_price == 0.0


def test_book_that_exactly_covers_the_budget_fills():
    """Boundary: $3.00 of depth at 0.25 is exactly 12 shares. Must fill, not
    trip the thin-book epsilon."""
    r = simulate_buy(book(asks=[(0.25, 12.0)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert r.filled
    assert r.shares == pytest.approx(12.0)


def test_one_cent_short_of_the_budget_is_a_skip():
    r = simulate_buy(book(asks=[(0.25, 11.96)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert not r.filled
    assert r.skip_reason is SkipReason.BOOK_TOO_THIN


# --- Price cap is on the AVERAGE, not on any single level ------------------

def test_average_above_cap_is_skipped_even_though_best_ask_is_cheap():
    """Best ask 0.10 looks fine, but the depth is at 0.90, so the average is
    ~0.71. The spec tests the resulting average, so this must skip."""
    b = book(asks=[(0.10, 2), (0.90, 1000)])
    r = simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50)
    assert b.best_ask == pytest.approx(0.10)
    assert not r.filled
    assert r.skip_reason is SkipReason.FILL_ABOVE_MAX
    assert r.would_be_avg_price > 0.50
    assert "0.90" in r.detail or "average fill" in r.detail


def test_fill_just_under_the_cap_is_accepted():
    r = simulate_buy(book(asks=[(0.49, 1000)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert r.filled
    assert r.avg_price == pytest.approx(0.49)


def test_fill_just_over_the_cap_is_rejected():
    r = simulate_buy(book(asks=[(0.51, 1000)]), 3.00, NO_FEE, max_fill_price=0.50)
    assert not r.filled
    assert r.skip_reason is SkipReason.FILL_ABOVE_MAX


# --- Minimum order size ----------------------------------------------------

def test_below_min_order_size_is_skipped():
    """At 0.80 a $3 order is 3.75 shares, under the 5-share exchange minimum.
    A residual that small is unsellable live."""
    r = simulate_buy(book(asks=[(0.80, 1000)], min_size=5.0), 3.00, NO_FEE,
                     max_fill_price=0.90)
    assert not r.filled
    assert r.skip_reason is SkipReason.BELOW_MIN_ORDER_SIZE
    assert r.min_order_size == 5.0


def test_min_order_size_can_be_disabled():
    r = simulate_buy(book(asks=[(0.80, 1000)], min_size=5.0), 3.00, NO_FEE,
                     max_fill_price=0.90, respect_min_order_size=False)
    assert r.filled
    assert r.shares == pytest.approx(3.75)


# --- Fees ------------------------------------------------------------------

def test_fee_formula_matches_polymarkets_worked_example():
    """Docs: 100 crypto shares at 50c costs $1.75 taker fee."""
    assert taker_fee_usd(100, 0.50, 0.07) == pytest.approx(1.75)
    # The older min(p, 1-p) form would give 3.50 -- confirm we are not using it.
    assert taker_fee_usd(100, 0.50, 0.07) != pytest.approx(3.50)


def test_fee_as_share_of_notional_is_large_at_cheap_prices():
    """The reason a flat bps model is the wrong shape: at 5c the fee is 4.75%
    of the money, not a rounding error."""
    assert effective_rate_of_notional(0.05, 0.05) == pytest.approx(0.0475)
    assert effective_rate_of_notional(0.50, 0.05) == pytest.approx(0.025)


def test_budget_is_total_outlay_including_fee():
    """'Every copy spends a fixed $3.00. Not more, not less.'"""
    r = simulate_buy(book(asks=[(0.25, 1000)]), 3.00, WEATHER_FEE, max_fill_price=0.50)
    assert r.filled
    assert r.gross_usd + r.fee_usd == pytest.approx(3.00)
    assert -r.net_usd == pytest.approx(3.00)
    assert r.shares < 12.0  # fee-free would have been exactly 12


def test_fee_is_charged_per_level_not_on_the_average():
    """p(1-p) is concave, so charging on the VWAP overstates the true fee.
    Per-level must come out strictly lower across a multi-level walk."""
    r = simulate_buy(book(asks=[(0.20, 5), (0.25, 5), (0.30, 100)]), 3.00,
                     WEATHER_FEE, max_fill_price=0.50)
    assert r.filled
    on_vwap = taker_fee_usd(r.shares, r.avg_price, WEATHER_FEE.rate)
    assert r.fee_usd < on_vwap
    assert r.fee_usd == pytest.approx(on_vwap, rel=0.05)  # same ballpark, not equal


def test_zero_fee_and_fee_produce_different_share_counts():
    kw = dict(max_fill_price=0.50)
    free = simulate_buy(book(asks=[(0.05, 10000)]), 3.00, NO_FEE, **kw)
    paid = simulate_buy(book(asks=[(0.05, 10000)]), 3.00, WEATHER_FEE, **kw)
    assert free.shares > paid.shares
    # At 5c the fee is 4.75% of notional, so ~4.5% fewer shares.
    assert paid.shares / free.shares == pytest.approx(1 / 1.0475, rel=1e-6)


def test_bps_override_is_a_different_and_flatter_shape():
    flat = FeeModel(rate=0.05, bps_override=50)  # 0.50% of notional
    r = simulate_buy(book(asks=[(0.05, 10000)]), 3.00, flat, max_fill_price=0.50)
    assert r.filled
    assert r.fee_usd == pytest.approx(r.gross_usd * 0.005)


# --- No rounding -----------------------------------------------------------

def test_sub_cent_prices_are_not_rounded_to_cents():
    """At a 0.003 price, rounding to a whole cent is a 233% error."""
    r = simulate_buy(book(asks=[(0.003, 10000)], tick=0.001), 3.00, NO_FEE,
                     max_fill_price=0.50)
    assert r.filled
    assert r.avg_price == pytest.approx(0.003)
    assert r.shares == pytest.approx(1000.0)


def test_vwap_is_not_snapped_to_the_tick_grid():
    """A weighted average across levels is legitimately off-grid."""
    r = simulate_buy(book(asks=[(0.010, 100), (0.011, 10000)], tick=0.001), 3.00,
                     NO_FEE, max_fill_price=0.50)
    assert r.filled
    grid_multiple = r.avg_price / 0.001
    assert abs(grid_multiple - round(grid_multiple)) > 1e-6


# --- No look-ahead ---------------------------------------------------------

def test_book_from_after_the_decision_is_rejected():
    future = book(asks=[(0.20, 1000)], ts=1_787_000_600_000)
    with pytest.raises(LookAheadError):
        simulate_buy(future, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000)


def test_book_from_before_the_decision_is_fine():
    past = book(asks=[(0.20, 1000)], ts=1_786_999_400_000)
    r = simulate_buy(past, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000)
    assert r.filled


# --- Selling ---------------------------------------------------------------
#
#   bids: 10 @ 0.40, 10 @ 0.30, 100 @ 0.10   sell 20 shares, no fees
#     10 @ 0.40 = 4.00,  10 @ 0.30 = 3.00  ->  20 shares for $7.00, VWAP 0.35

def test_sell_walks_the_bid_side_downwards():
    r = simulate_sell(book(bids=[(0.40, 10), (0.30, 10), (0.10, 100)]), 20.0, NO_FEE)
    assert r.filled
    assert r.gross_usd == pytest.approx(7.00)
    assert r.avg_price == pytest.approx(0.35)
    assert not r.is_partial


def test_sell_below_the_best_bid_when_eating_depth():
    r = simulate_sell(book(bids=[(0.40, 10), (0.30, 10), (0.10, 100)]), 20.0, NO_FEE)
    assert r.avg_price < 0.40


def test_sell_into_thin_book_is_partial_and_says_so():
    r = simulate_sell(book(bids=[(0.40, 8)]), 20.0, NO_FEE)
    assert r.filled
    assert r.is_partial
    assert r.shares == pytest.approx(8.0)
    assert r.requested_shares == pytest.approx(20.0)
    assert "partial" in r.detail


def test_sell_with_no_bids_skips():
    r = simulate_sell(book(bids=[]), 20.0, NO_FEE)
    assert not r.filled
    assert r.skip_reason is SkipReason.EMPTY_BOOK


def test_sell_below_min_order_size_skips():
    r = simulate_sell(book(bids=[(0.40, 3)], min_size=5.0), 20.0, NO_FEE)
    assert not r.filled
    assert r.skip_reason is SkipReason.BELOW_MIN_ORDER_SIZE


def test_sell_fee_reduces_proceeds():
    free = simulate_sell(book(bids=[(0.05, 10000)]), 100.0, NO_FEE)
    paid = simulate_sell(book(bids=[(0.05, 10000)]), 100.0, WEATHER_FEE)
    assert paid.net_usd < free.net_usd
    assert paid.fee_usd == pytest.approx(100 * 0.05 * 0.95 * 0.05)
    assert paid.net_usd == pytest.approx(free.net_usd - paid.fee_usd)
