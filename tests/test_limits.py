"""Resting limit orders.

The failure mode being guarded against: assuming a fill because the ask touched
our price. The ask *quoting* at our limit is not the market *trading* at it, and
it says nothing about the queue in front of us. Fills come from executed prints
or they do not happen.
"""
import pytest

from copybot import limits
from copybot.fees import FeeModel
from copybot.models import BookLevel, OrderBook

NO_FEE = FeeModel(rate=0.0)
WEATHER = FeeModel(rate=0.05)
T0 = 1_787_000_000


def book(bids=(), asks=()):
    return OrderBook(
        token_id="TOK", condition_id="0xc",
        bids=sorted([BookLevel(p, s) for p, s in bids], key=lambda l: l.price, reverse=True),
        asks=sorted([BookLevel(p, s) for p, s in asks], key=lambda l: l.price),
        tick_size=0.001, min_order_size=5.0,
    )


def print_(price, size, ts, side="SELL", wallet="0xstranger"):
    return {"price": str(price), "size": str(size), "timestamp": ts,
            "side": side, "proxyWallet": wallet}


def order(b, his_price=0.03, usd=3.0, ttl=300):
    return limits.place(b, his_price, usd, now=T0, ttl_seconds=ttl)


# --- queue position --------------------------------------------------------

def test_queue_counts_our_price_and_better():
    b = book(bids=[(0.03, 100), (0.02, 500), (0.04, 50)])
    # bids at 0.04 and 0.03 have priority; 0.02 is behind us
    assert limits.queue_ahead_shares(b, 0.03) == pytest.approx(150)


def test_empty_bid_side_means_no_queue():
    assert limits.queue_ahead_shares(book(bids=[]), 0.03) == 0.0


def test_target_shares_from_budget_and_his_price():
    o = order(book(bids=[(0.03, 100)]), his_price=0.03, usd=3.0)
    assert o.target_shares == pytest.approx(100.0)
    assert o.queue_ahead_shares == pytest.approx(100.0)


# --- the trap this module exists to avoid ----------------------------------

def test_ask_touching_our_price_is_not_a_fill():
    """An ask quoted at our limit means someone is OFFERING there. It does not
    mean anyone traded, and it does not move us up the queue."""
    b = book(bids=[(0.03, 100)], asks=[(0.03, 5000)])
    o = order(b, his_price=0.03)
    limits.apply_tape(o, [], NO_FEE, T0 + 60)
    assert o.filled_shares == 0.0
    assert o.fill_fraction == 0.0


def test_no_prints_means_no_fill():
    o = order(book(bids=[(0.03, 10)]), his_price=0.03)
    limits.apply_tape(o, [], NO_FEE, T0 + 299)
    assert o.filled_shares == 0.0
    assert "unfilled" in limits.describe(o)


# --- filling ---------------------------------------------------------------

def test_queue_must_clear_before_we_fill():
    """100 ahead of us. 80 trades through -> still nothing for us."""
    o = order(book(bids=[(0.03, 100)]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 80, T0 + 10)], NO_FEE, T0 + 20)
    assert o.consumed_shares == pytest.approx(80)
    assert o.filled_shares == 0.0


def test_fill_starts_once_the_queue_is_consumed():
    """100 ahead, 160 trades through -> 60 shares reach us."""
    o = order(book(bids=[(0.03, 100)]), his_price=0.03)   # target 100 shares
    limits.apply_tape(o, [print_(0.03, 160, T0 + 10)], NO_FEE, T0 + 20)
    assert o.filled_shares == pytest.approx(60)
    assert o.filled_usd == pytest.approx(60 * 0.03)
    assert o.fill_fraction == pytest.approx(0.60)
    assert "partial" in limits.describe(o)


def test_fill_never_exceeds_the_order():
    o = order(book(bids=[(0.03, 100)]), his_price=0.03)   # target 100
    limits.apply_tape(o, [print_(0.03, 10_000, T0 + 5)], NO_FEE, T0 + 10)
    assert o.filled_shares == pytest.approx(100)
    assert o.is_complete
    assert "filled" in limits.describe(o)


def test_no_queue_means_immediate_participation():
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 40, T0 + 5)], NO_FEE, T0 + 10)
    assert o.filled_shares == pytest.approx(40)


def test_fills_accumulate_across_polls():
    o = order(book(bids=[(0.03, 50)]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 70, T0 + 10)], NO_FEE, T0 + 15)
    assert o.filled_shares == pytest.approx(20)
    limits.apply_tape(o, [print_(0.03, 70, T0 + 10), print_(0.03, 30, T0 + 40)],
                      NO_FEE, T0 + 45)
    assert o.filled_shares == pytest.approx(50)


# --- what must NOT count ---------------------------------------------------

def test_prints_above_our_limit_are_ignored():
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.05, 1000, T0 + 10)], NO_FEE, T0 + 20)
    assert o.filled_shares == 0.0


def test_prints_below_our_limit_do_count():
    """A seller taking 0.02 would have preferred our 0.03 bid, so that volume
    passed through our price level."""
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.02, 40, T0 + 10)], NO_FEE, T0 + 20)
    assert o.filled_shares == pytest.approx(40)


def test_prints_before_placement_are_ignored():
    """Using them would be look-ahead in reverse."""
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 500, T0 - 60)], NO_FEE, T0 + 20)
    assert o.filled_shares == 0.0


def test_prints_after_expiry_are_ignored():
    o = order(book(bids=[]), his_price=0.03, ttl=300)
    limits.apply_tape(o, [print_(0.03, 500, T0 + 900)], NO_FEE, T0 + 1000)
    assert o.filled_shares == 0.0
    assert o.expired(T0 + 1000)


def test_malformed_prints_do_not_crash_the_fill():
    o = order(book(bids=[]), his_price=0.03)
    tape = [{"price": "oops", "size": "5", "timestamp": T0 + 1},
            {"size": "5", "timestamp": T0 + 1},
            print_(0.03, 25, T0 + 2)]
    limits.apply_tape(o, tape, NO_FEE, T0 + 10)
    assert o.filled_shares == pytest.approx(25)


# --- fees ------------------------------------------------------------------

def test_maker_fill_pays_no_fee():
    """Every live schedule sampled carries takerOnly: true."""
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 200, T0 + 5)], WEATHER, T0 + 10)
    assert o.filled_shares > 0
    assert o.fee_usd == 0.0
    assert o.filled_usd == pytest.approx(o.filled_shares * 0.03)


def test_resting_beats_crossing_on_the_same_pick():
    """The whole point. He paid 3c; crossing the book costs 8.9c."""
    from copybot.fills import simulate_buy
    b = book(bids=[(0.029, 500)], asks=[(0.089, 10_000)])
    crossed = simulate_buy(b, 3.00, WEATHER, max_fill_price=0.50)
    o = limits.place(b, 0.03, 3.00, now=T0, ttl_seconds=300)
    limits.apply_tape(o, [print_(0.03, 200, T0 + 30)], WEATHER, T0 + 60)
    assert crossed.filled and crossed.avg_price == pytest.approx(0.089)
    assert o.filled_shares > 0
    assert o.limit_price == pytest.approx(0.03)
    assert crossed.avg_price / o.limit_price == pytest.approx(2.97, abs=0.01)
    assert crossed.avg_price / o.limit_price > 1.395, "crossing blows the break-even ratio"


# --- marketable ------------------------------------------------------------

def test_marketable_when_the_ask_is_at_or_below_our_limit():
    assert limits.marketable(book(asks=[(0.03, 100)]), 0.03)
    assert limits.marketable(book(asks=[(0.02, 100)]), 0.03)
    assert not limits.marketable(book(asks=[(0.04, 100)]), 0.03)
    assert not limits.marketable(book(asks=[]), 0.03)


# --- only seller-initiated prints, and never his own -----------------------

def test_buyer_lifting_an_ask_does_not_fill_our_bid():
    """A BUY print at our price consumed ask-side liquidity, not our queue.
    Counting it would inflate the fill rate -- the one number that decides
    whether this strategy is reachable."""
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 500, T0 + 10, side="BUY")], NO_FEE, T0 + 20)
    assert o.filled_shares == 0.0


def test_his_own_print_never_fills_us():
    """His buy appears on the same tape. If it filled us we would be trading
    against the person we are copying."""
    o = order(book(bids=[]), his_price=0.03)
    tape = [print_(0.03, 500, T0 + 10, side="SELL", wallet="0xHIM")]
    limits.apply_tape(o, tape, NO_FEE, T0 + 20, exclude_wallet="0xhim")
    assert o.filled_shares == 0.0


def test_a_stranger_selling_does_fill_us():
    o = order(book(bids=[]), his_price=0.03)
    tape = [print_(0.03, 40, T0 + 10, side="SELL", wallet="0xsomeone")]
    limits.apply_tape(o, tape, NO_FEE, T0 + 20, exclude_wallet="0xhim")
    assert o.filled_shares == pytest.approx(40)


def test_relaxing_the_sell_requirement_counts_both():
    o = order(book(bids=[]), his_price=0.03)
    limits.apply_tape(o, [print_(0.03, 40, T0 + 10, side="BUY")], NO_FEE, T0 + 20,
                      require_sell_prints=False)
    assert o.filled_shares == pytest.approx(40)
