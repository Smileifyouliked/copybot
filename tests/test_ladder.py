"""Shadow size ladder, full mark path, and the look-ahead lag bound."""
import pytest

from conftest import FakeClient, FakeExecutor, make_book, make_meta, make_trade

from copybot.analytics import capacity_curve, clv_at_horizons, depth_cost_summary
from copybot.fees import FeeModel
from copybot.fills import LookAheadError, simulate_buy, size_ladder
from copybot.models import BookLevel, OrderBook
from copybot.strategy import Strategy

NO_FEE = FeeModel(rate=0.0)


def book(asks=(), bids=(), min_size=5.0, ts=0):
    return OrderBook(
        token_id="TOK1", condition_id="0xcond1",
        bids=sorted([BookLevel(p, s) for p, s in bids], key=lambda l: l.price, reverse=True),
        asks=sorted([BookLevel(p, s) for p, s in asks], key=lambda l: l.price),
        tick_size=0.001, min_order_size=min_size, timestamp_ms=ts,
    )


RUNGS = [("$1", 1.0), ("$3", 3.0), ("$10", 10.0)]


# --- The ladder itself -----------------------------------------------------

def test_ladder_shows_cost_rising_with_size():
    """20 @ 0.10 ($2.00), 20 @ 0.20 ($4.00), 200 @ 0.40. Bigger orders reach
    worse levels, and the ladder is exactly that curve."""
    b = book(asks=[(0.10, 20), (0.20, 20), (0.40, 200)])
    rungs = {r.label: r for r in size_ladder(b, RUNGS, NO_FEE, max_fill_price=0.50)}

    # $1 fits inside the top level: 10 shares at 0.10, no depth cost.
    assert rungs["$1"].vwap == pytest.approx(0.10)
    assert rungs["$1"].depth_cost_pct == pytest.approx(0.0)
    assert rungs["$1"].levels_consumed == 1

    # $3: 20@0.10 = $2.00, then $1.00/0.20 = 5 shares -> 25 shares, VWAP 0.12
    assert rungs["$3"].vwap == pytest.approx(0.12)
    assert rungs["$3"].depth_cost_pct == pytest.approx(20.0)
    assert rungs["$3"].levels_consumed == 2

    # $10: $2.00 + $4.00, then $4.00/0.40 = 10 shares -> 50 shares, VWAP 0.20
    assert rungs["$10"].vwap == pytest.approx(0.20)
    assert rungs["$10"].depth_cost_pct == pytest.approx(100.0)
    assert rungs["$10"].levels_consumed == 3

    assert rungs["$10"].vwap > rungs["$3"].vwap > rungs["$1"].vwap


def test_ladder_separates_depth_failure_from_price_failure():
    """A rung the book CAN absorb still reports its VWAP even when that VWAP is
    unacceptable -- otherwise 'too expensive' and 'too thin' collapse together."""
    b = book(asks=[(0.10, 5), (0.90, 1000)])
    rungs = {r.label: r for r in size_ladder(b, RUNGS, NO_FEE, max_fill_price=0.50)}
    assert rungs["$10"].filled is True          # depth exists
    assert rungs["$10"].cleared_max_fill is False  # but the price is unacceptable
    assert rungs["$10"].vwap > 0.50
    assert rungs["$10"].skip_reason is None


def test_ladder_marks_rungs_the_book_cannot_absorb():
    b = book(asks=[(0.10, 5)])  # $0.50 of depth
    rungs = {r.label: r for r in size_ladder(b, RUNGS, NO_FEE, max_fill_price=0.50)}
    assert rungs["$1"].filled is False
    assert rungs["$1"].skip_reason == "book_too_thin_to_fill_stake"
    assert rungs["$3"].filled is False


def test_ladder_on_an_empty_book_does_not_crash():
    rungs = size_ladder(book(asks=[]), RUNGS, NO_FEE, max_fill_price=0.50)
    assert len(rungs) == 3
    assert all(not r.filled for r in rungs)
    assert all(r.depth_cost_pct is None for r in rungs)


def test_ladder_never_touches_cash_or_positions(db, single_cfg):
    ex = FakeExecutor(books={"TOK1": book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])})
    cl = FakeClient(metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    before = db.cash()
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    # One copy happened, so cash moved by exactly one stake and no more.
    assert db.cash() == pytest.approx(before - 3.0)
    assert len(db.open_positions()) == 1
    assert db.conn.execute("SELECT COUNT(*) c FROM shadow_fills").fetchone()["c"] >= 3


# --- Ladder recorded on skips too ------------------------------------------

def test_ladder_is_recorded_even_when_we_skip(db, single_cfg):
    """A skip is exactly when the capacity curve matters most."""
    ex = FakeExecutor(books={"TOK1": book(asks=[(0.60, 10_000)], bids=[(0.59, 10_000)])})
    cl = FakeClient(metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.05, ts=1_787_000_000)])

    assert db.open_positions() == []
    rows = db.conn.execute(
        "SELECT rung_label, outcome, cleared_max_fill FROM shadow_fills").fetchall()
    assert len(rows) >= 3
    assert all(r["outcome"].startswith("skipped:") for r in rows)
    assert all(r["cleared_max_fill"] == 0 for r in rows)


def test_his_own_size_is_a_rung(db, single_cfg):
    ex = FakeExecutor(books={"TOK1": book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])})
    cl = FakeClient(metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.20, shares=10.0, ts=1_787_000_000)])  # $2.00
    labels = {r["rung_label"] for r in
              db.conn.execute("SELECT DISTINCT rung_label FROM shadow_fills")}
    assert {"$1", "$3", "$10", "his_fill", "his_position"} <= labels
    his = db.conn.execute(
        "SELECT rung_usd FROM shadow_fills WHERE rung_label='his_fill'").fetchone()
    assert his["rung_usd"] == pytest.approx(2.00)


def test_capacity_curve_rolls_the_rungs_up(db, single_cfg):
    ex = FakeExecutor(books={"TOK1": book(asks=[(0.05, 40), (0.40, 10_000)],
                                          bids=[(0.04, 500)])})
    cl = FakeClient(metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.05, ts=1_787_000_000)])

    curve = {r["rung"]: r for r in capacity_curve(db)}
    # $1 fits inside the cheap level; $10 has to reach the 0.40 level.
    assert curve["$1"]["median_depth_cost_pct"] == pytest.approx(0.0)
    assert curve["$10"]["median_depth_cost_pct"] > curve["$1"]["median_depth_cost_pct"]
    assert curve["$1"]["clear_rate"] == 1.0


def test_depth_cost_summary_buckets(db, single_cfg):
    ex = FakeExecutor(books={"TOK1": book(asks=[(0.05, 40), (0.40, 10_000)],
                                          bids=[(0.04, 500)])})
    cl = FakeClient(metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.05, ts=1_787_000_000)])
    summary = depth_cost_summary(db, "$3")
    assert summary["n"] == 1
    assert summary["median"] is not None
    assert sum(b["n"] for b in summary["buckets"]) == summary["n"]


# --- Full mark path --------------------------------------------------------

def test_every_mark_is_its_own_row(db, single_cfg):
    books = {"TOK1": book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])
    pos_id = db.open_positions()[0]["id"]

    for offset in (300, 600, 900):
        Strategy(single_cfg, db, ex, cl, clock=lambda o=offset: 1_787_000_000 + o).mark_positions()

    marks = db.marks_for(pos_id)
    assert len(marks) == 4  # entry + three marks
    assert [m["ts"] for m in marks] == sorted(m["ts"] for m in marks)
    assert marks[-1]["bid"] == pytest.approx(0.19)
    assert marks[-1]["ask"] == pytest.approx(0.20)
    assert marks[-1]["spread"] == pytest.approx(0.01)


def test_clv_at_horizons_uses_the_mark_path(db, single_cfg):
    books = {"TOK1": book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])

    # Price drifts up; mark at roughly entry + 15 minutes.
    cl.books["TOK1"] = book(asks=[(0.30, 10_000)], bids=[(0.28, 10_000)])
    Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_900).mark_positions()

    rows = {r["horizon_minutes"]: r for r in clv_at_horizons(db, [15, 60])}
    assert rows[15]["measured"] == 1
    assert rows[15]["mean_clv_pct"] > 0      # mid 0.29 vs entry 0.20
    assert rows[15]["positive"] == 1
    assert rows[60]["measured"] == 0, "no mark near +60m, so nothing to report"
    assert rows[60]["mean_clv_pct"] is None


def test_horizon_clv_ignores_gamma_marks(db, single_cfg):
    """A gamma price for a resolved market is the outcome, not a market price."""
    books = {"TOK1": book(asks=[(0.20, 10_000)], bids=[(0.19, 10_000)])}
    ex = FakeExecutor(books=books)
    cl = FakeClient(books=books, metas={"0xcond1": make_meta()})
    s = Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_000)
    s.process_trades([make_trade(price=0.20, ts=1_787_000_000)])

    cl.books["TOK1"] = book(asks=[], bids=[])
    cl.metas["0xcond1"] = make_meta(prices=(1.0, 0.0), closed=True)
    Strategy(single_cfg, db, ex, cl, clock=lambda: 1_787_000_900).mark_positions()

    rows = {r["horizon_minutes"]: r for r in clv_at_horizons(db, [15])}
    assert rows[15]["measured"] == 0


# --- Look-ahead lag bound --------------------------------------------------

def test_book_later_in_the_same_second_is_rejected_when_strict():
    """The hole this closes: a book stamped 400ms after the decision used to
    truncate to the same whole second and pass."""
    b = book(asks=[(0.20, 10_000)], ts=1_787_000_000_400)
    with pytest.raises(LookAheadError):
        simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000, max_book_lag_seconds=0.0)


def test_book_at_exactly_the_decision_instant_is_rejected_when_strict():
    b = book(asks=[(0.20, 10_000)], ts=1_787_000_000_000)
    with pytest.raises(LookAheadError):
        simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000, max_book_lag_seconds=0.0)


def test_book_provably_earlier_passes_when_strict():
    b = book(asks=[(0.20, 10_000)], ts=1_786_999_999_999)
    r = simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000, max_book_lag_seconds=0.0)
    assert r.filled
    assert r.book_lag_ms == pytest.approx(-1.0)


def test_live_tolerance_admits_a_fetch_round_trip():
    """Live fills legitimately use a book fetched a few hundred ms after we
    decide -- that is the honest model, not look-ahead."""
    b = book(asks=[(0.20, 10_000)], ts=1_787_000_000_400)
    r = simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000, max_book_lag_seconds=5.0)
    assert r.filled
    assert r.book_lag_ms == pytest.approx(400.0)


def test_book_far_after_the_decision_is_rejected_even_when_lenient():
    b = book(asks=[(0.20, 10_000)], ts=1_787_000_600_000)  # 10 minutes later
    with pytest.raises(LookAheadError):
        simulate_buy(b, 3.00, NO_FEE, max_fill_price=0.50,
                     decision_ts=1_787_000_000, max_book_lag_seconds=5.0)


def test_unplaceable_rung_still_reports_its_depth_cost():
    """His own trade sizes are often under the 5-share floor once fees come out
    of the same budget. That must not throw away the depth measurement -- the
    rung at his size is the sharpest one on the ladder."""
    b = book(asks=[(0.03, 240)], min_size=5.0)
    rungs = size_ladder(b, [("his_fill", 0.15)], FeeModel(rate=0.05),
                        max_fill_price=0.50)
    rung = rungs[0]
    assert rung.filled is True
    assert rung.shares < 5.0
    assert rung.below_min_order_size is True
    assert rung.cleared_max_fill is False       # unplaceable, so it does not clear
    assert rung.vwap == pytest.approx(0.03)     # but the price is still measured
    assert rung.depth_cost_pct == pytest.approx(0.0)
    assert rung.levels_consumed == 1


def test_placeable_rung_is_not_flagged():
    b = book(asks=[(0.03, 240)], min_size=5.0)
    rung = size_ladder(b, [("$3", 3.0)], FeeModel(rate=0.05), max_fill_price=0.50)[0]
    assert rung.below_min_order_size is False
    assert rung.cleared_max_fill is True
